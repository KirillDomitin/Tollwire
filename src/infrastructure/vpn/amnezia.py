#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import ClassVar

from core.ssh_exception import (
    CommandExecutionError,
    DockerError,
    ForeignContainerError,
    InstallError,
    InvalidServerConfigurationError,
)
from infrastructure.ssh.client import SSHClient
from infrastructure.vpn.amnezia_link import amnezia_qr_codes, amnezia_share_link
from infrastructure.vpn.awg_config import (
    append_peer,
    ensure_nat_hooks,
    generate_secret_keys,
    interface_values,
    is_valid_public_key,
    list_peers,
    parse_client_config,
    parse_latest_handshakes,
    pick_free_address,
    pick_udp_port,
    remove_peer,
    render_awg_config,
    render_client_config,
    render_peer_block,
    used_peer_addresses,
)
from infrastructure.vpn.config import (
    ACTIVE_HANDSHAKE_WINDOW_SECONDS,
    AMNEZIAWG_IMAGE,
    DEFAULT_LAYOUT,
    INTERNET_PROBE_COMMAND,
    RESTART_POLICY,
    RESTARTING_POLICIES,
    ContainerLayout,
)
from infrastructure.vpn.provider import (
    HealthCheck,
    ProviderHealth,
    ProviderState,
    ProviderStatus,
    SyncResult,
    VPNClientConfig,
    VPNClientInfo,
    VPNProvider,
)

logger = logging.getLogger("vpn.amnezia")

# fullmatch (см. validate_name): у re.match «$» пропускает завершающий \n, а имя пишется в awg0.conf.
_ALLOWED = re.compile(r"[a-zA-Z0-9_-]{1,32}")

# apt-get update + install на свежем сервере — минуты (с общим SSH_COMMAND_TIMEOUT установка падала, а apt на
# сервере продолжал работу и доставлял Docker уже без панели).
DOCKER_INSTALL_TIMEOUT = 600


def validate_name(name: str) -> None:
    if not _ALLOWED.fullmatch(name):
        raise ValueError("Name must be 1-32 characters long and contain only letters, numbers, underscores, or hyphens")


class AmneziaInstaller:
    def __init__(self, ssh_client: SSHClient):
        self.ssh_client = ssh_client

    async def _validate_server(self) -> None:
        check_os = await self.ssh_client.execute("uname -s")
        if check_os.exit_status != 0:
            raise CommandExecutionError(f"Command failed: {check_os.stderr}")
        if check_os.stdout.strip() != "Linux":
            raise InvalidServerConfigurationError(
                "Incompatible operating system detected. This application requires a Linux environment to run."
            )

        check_root = await self.ssh_client.execute("id -u")
        if check_root.exit_status != 0:
            raise CommandExecutionError(f"Command failed: {check_root.stderr}")
        if check_root.stdout.strip() != "0":
            raise InvalidServerConfigurationError("Root privileges are required to run this application.")

    async def _ensure_docker(self) -> None:
        check_docker = await self.ssh_client.execute("command -v docker")
        if check_docker.exit_status != 0:
            install_docker = await self.ssh_client.execute(
                "apt-get update && apt-get install -y docker.io", timeout=DOCKER_INSTALL_TIMEOUT
            )
            if install_docker.exit_status != 0:
                raise InstallError(f"Docker installation crashed with the following error: {install_docker.stderr}")

        check_demon = await self.ssh_client.execute("systemctl is-active docker")
        if check_demon.stdout.strip() != "active":
            result = await self.ssh_client.execute("systemctl start docker")
            if result.exit_status != 0:
                raise DockerError(f"Failed to start Docker: {result.stderr.strip()}")

            result = await self.ssh_client.execute("systemctl enable docker")
            if result.exit_status != 0:
                logger.warning(
                    "docker started, but autostart was not enabled", extra={"exit_status": result.exit_status}
                )

    async def docker_version(self) -> str | None:
        """Версия Docker на сервере или None, если Docker не установлен. Ничего не меняет."""
        result = await self.ssh_client.execute("docker --version")
        if result.exit_status != 0:
            return None
        return result.stdout.strip() or None

    async def docker_daemon_active(self) -> bool:
        result = await self.ssh_client.execute("systemctl is-active docker")
        return result.stdout.strip() == "active"

    async def internet_latency_ms(self) -> int | None:
        """Время подключения сервера к внешнему адресу в мс; None — интернета нет или curl недоступен."""
        result = await self.ssh_client.execute(INTERNET_PROBE_COMMAND, timeout=10)
        if result.exit_status != 0:
            return None
        try:
            seconds = float(result.stdout.strip())
        except ValueError:
            return None
        return round(seconds * 1000) if seconds > 0 else None

    async def ensure_ready(self) -> None:
        await self._validate_server()
        await self._ensure_docker()


class AmneziaWGProvider(VPNProvider):
    # Раскладка контейнера на сервере: имя, папки, интерфейс, подсеть, DNS. Все команды собираются только из неё;
    # подкласс (например, шлюз) переопределяет её целиком и получает всю логику провайдера с другими именами.
    layout: ClassVar[ContainerLayout] = DEFAULT_LAYOUT

    def __init__(self, ssh_client: SSHClient):
        super().__init__(ssh_client)
        self.ssh_client = ssh_client
        self.installer = AmneziaInstaller(ssh_client)

    @classmethod
    def share_link(cls, client_config: str, display_name: str) -> str | None:
        """Строка vpn:// для приложения AmneziaVPN; display_name приложение покажет как имя сервера."""
        return amnezia_share_link(
            client_config, display_name, subnet=cls.layout.tunnel_subnet, dns=cls.layout.client_dns
        )

    @classmethod
    def share_qr_codes(cls, client_config: str, display_name: str) -> list[str] | None:
        """Серия QR в формате экспорта AmneziaVPN: одиночный код со всей строкой vpn:// камера не читает."""
        return amnezia_qr_codes(client_config, display_name, subnet=cls.layout.tunnel_subnet, dns=cls.layout.client_dns)

    def _ps(self, field: str) -> str:
        """`docker ps` по точному имени нашего контейнера; field — State / Status / Image."""
        return f"docker ps -a --filter {self.layout.container_filter} --format '{{{{.{field}}}}}'"

    async def _is_foreign(self) -> bool:
        """Контейнер с нашим именем есть, но создан не панелью.

        Имя `amnezia-awg` у приложения AmneziaVPN то же самое. Свой контейнер всегда монтирует layout.host_config_dir
        (см. _run_container), у чужого такого монтирования нет. Источники сравниваются точно, не по префиксу.
        Любое изменение (остановка, удаление, правка конфига) чужого контейнера уничтожило бы чужой VPN.
        Сбой самой проверки — ошибка, а не «контейнера нет»: решать вслепую нельзя. Остаточное окно между проверкой
        и действием (контейнер подменили посреди операции) принято: от панели оно закрыто ServerLock, а
        деструктивные _run_container/_rm_container перепроверяют сами непосредственно перед docker run/start/rm.
        """
        layout = self.layout
        state = await self.ssh_client.execute(self._ps("State"))
        if state.exit_status != 0:
            raise CommandExecutionError(f"Failed to list containers: {state.stderr.strip()}")
        if not state.stdout.strip():
            return False
        # --type container: без него inspect мог бы вернуть образ или том с таким же именем.
        mounts_format = "'{{range .Mounts}}{{.Source}} {{end}}'"
        mounts = await self.ssh_client.execute(
            f"docker inspect --type container -f {mounts_format} {layout.container_name}"
        )
        if mounts.exit_status != 0:
            raise CommandExecutionError(
                f"Failed to inspect container '{layout.container_name}': {mounts.stderr.strip()}"
            )
        return layout.host_config_dir not in mounts.stdout.split()

    async def _require_own_container(self) -> None:
        if await self._is_foreign():
            raise ForeignContainerError(
                f"Container '{self.layout.container_name}' on this server was not created by the panel "
                "(probably by the AmneziaVPN application); the panel does not touch it"
            )

    async def install(self) -> ProviderStatus:
        await self._require_own_container()
        layout = self.layout
        test = await self.ssh_client.execute(f"test -f {layout.host_config_file}")
        if test.exit_status == 0:
            # Повторная установка чинит серверы, поставленные до появления NAT-хуков.
            await self._ensure_nat()
            await self._ensure_restart_policy()
            return await self.get_status()
        port = pick_udp_port()
        keys = generate_secret_keys()
        config = render_awg_config(
            keys.private_key, port, tunnel_subnet=layout.tunnel_subnet, address=layout.interface_address
        )
        await self.ssh_client.write_file(layout.host_config_file, config)
        await self._run_container(port)
        return await self.get_status()

    async def uninstall(self) -> ProviderStatus:
        if await self._is_foreign():
            return await self.get_status()  # чужой контейнер не наш: удалять нечего, статус — «не установлен»
        await self._rm_container()
        return await self.get_status()

    async def get_status(self) -> ProviderStatus:
        if await self._is_foreign():
            return ProviderStatus(server_status=ProviderState.NOT_INSTALLED, version=None, port=None, uptime=None)
        layout = self.layout
        status = await self.ssh_client.execute(self._ps("State"))
        if status.exit_status == 0 and status.stdout.strip():
            raw_state = status.stdout.strip().lower()

            if raw_state == "running":
                status_result = ProviderState.RUNNING
            elif raw_state in ("exited", "created", "paused"):
                status_result = ProviderState.STOPPED
            else:
                status_result = ProviderState.STOPPED
        else:
            status_result = ProviderState.NOT_INSTALLED
            return ProviderStatus(server_status=status_result, version=None, port=None, uptime=None)

        port = await self.ssh_client.execute(f"grep 'ListenPort' {layout.host_config_file} | cut -d' ' -f3")
        if port.exit_status == 0:
            port_str = port.stdout.strip()
            port_result = int(port_str)

        elif port.exit_status == 1:
            raise CommandExecutionError("Error: 'ListenPort' key not found in the configuration file.")

        elif port.exit_status == 2:
            raise CommandExecutionError(f"Error: Configuration file at {layout.host_config_file} does not exist.")

        else:
            raise CommandExecutionError(f"Unexpected error (Code {port.exit_status}): {port.stderr.strip()}")

        uptime = await self.ssh_client.execute(self._ps("Status"))
        version = await self.ssh_client.execute(self._ps("Image"))
        if uptime.exit_status == 0 and uptime.stdout.strip():
            uptime_result = uptime.stdout.strip()
        else:
            raise CommandExecutionError(
                f"Error or Container '{layout.container_name}' not found while checking uptime."
            )

        if version.exit_status == 0 and version.stdout.strip():
            version_result = version.stdout.strip()
        else:
            raise CommandExecutionError(
                f"Error or Container '{layout.container_name}' not found while checking version."
            )

        return ProviderStatus(
            server_status=status_result, version=version_result, port=port_result, uptime=uptime_result
        )

    async def create_client(self, name: str) -> VPNClientConfig:
        """Добавляет клиента на сервер и возвращает конфиг для его приложения.

        Делает read-modify-write серверного awg0.conf — вызывающий обязан держать ServerLock.
        """
        validate_name(name)
        await self._require_own_container()
        server_config = await self.ssh_client.read_file(self.layout.host_config_file)
        address = pick_free_address(server_config)
        keys = generate_secret_keys()
        server_public_key = await self._server_public_key()

        updated = append_peer(server_config, render_peer_block(name, keys.public_key, keys.psk, address))
        await self.ssh_client.write_file(self.layout.host_config_file, updated)
        await self._apply_config()

        client_config = render_client_config(
            server_config=server_config,
            client_private_key=keys.private_key,
            client_address=address,
            psk=keys.psk,
            server_public_key=server_public_key,
            endpoint_host=self.ssh_client.host,
            dns=self.layout.client_dns,
        )
        return VPNClientConfig(name=name, public_key=keys.public_key, address=str(address), config=client_config)

    async def revoke_client(self, public_key: str) -> None:
        """Убирает клиента с сервера. Идемпотентно: отсутствующий клиент — не ошибка.

        Вызывающий обязан держать ServerLock (read-modify-write awg0.conf).
        """
        if not is_valid_public_key(public_key):
            raise ValueError("Invalid WireGuard public key")
        await self._require_own_container()
        server_config = await self.ssh_client.read_file(self.layout.host_config_file)
        updated, removed = remove_peer(server_config, public_key)
        if not removed:
            return
        await self.ssh_client.write_file(self.layout.host_config_file, updated)
        await self._apply_config()

    async def _ensure_nat(self) -> None:
        """Гарантирует выход клиентов в интернет: хуки NAT в конфиге и правило в работающем контейнере.

        Хуки сработают при следующем старте контейнера; чтобы не перезапускать его (и не рвать
        соединения клиентов), правило дополнительно применяется сразу. Обе операции идемпотентны.
        """
        layout = self.layout
        server_config = await self.ssh_client.read_file(layout.host_config_file)
        updated, changed = ensure_nat_hooks(server_config, layout.tunnel_subnet)
        if changed:
            await self.ssh_client.write_file(layout.host_config_file, updated)

        running = await self.ssh_client.execute(f"docker inspect -f '{{{{.State.Running}}}}' {layout.container_name}")
        if running.stdout.strip() != "true":
            return
        result = await self.ssh_client.execute(f"docker exec {layout.container_name} sh -c '{layout.nat_post_up}'")
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to apply NAT rule: {result.stderr.strip()}")

    async def _restart_policy(self) -> str | None:
        """Политика перезапуска нашего контейнера или None, если контейнера нет."""
        result = await self.ssh_client.execute(
            f"docker inspect -f '{{{{.HostConfig.RestartPolicy.Name}}}}' {self.layout.container_name}"
        )
        return result.stdout.strip() if result.exit_status == 0 else None

    async def _ensure_restart_policy(self) -> None:
        """Без политики перезапуска VPN не поднимется после перезагрузки сервера: контейнер так и останется
        остановленным. `docker update` меняет настройку на месте — контейнер не перезапускается, клиенты не рвутся.
        Политику, которая уже перезапускает контейнер (в том числе выбранную вручную), не трогаем."""
        policy = await self._restart_policy()
        if policy is None or policy in RESTARTING_POLICIES:
            return
        result = await self.ssh_client.execute(f"docker update --restart {RESTART_POLICY} {self.layout.container_name}")
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to set restart policy: {result.stderr.strip()}")

    async def _server_public_key(self) -> str:
        # Берём публичный ключ у живого интерфейса — приватный ключ сервера не покидает сервер.
        layout = self.layout
        result = await self.ssh_client.execute(
            f"docker exec {layout.container_name} awg show {layout.interface_name} public-key"
        )
        public_key = result.stdout.strip()
        if result.exit_status != 0 or not is_valid_public_key(public_key):
            raise CommandExecutionError(f"Failed to read server public key: {result.stderr.strip()}")
        return public_key

    async def _apply_config(self) -> None:
        # awg-quick strip убирает строки, которых не понимает awg (Address, DNS…), а syncconf применяет
        # конфиг к живому интерфейсу без перезапуска: существующие клиенты не отключаются.
        # Временный файл содержит приватный ключ сервера — удаляем при любом исходе.
        layout = self.layout
        stripped = f"/tmp/{layout.interface_name}.stripped"
        script = (
            f"awg-quick strip {layout.container_config_file} > {stripped}"
            f" && awg syncconf {layout.interface_name} {stripped}; rc=$?; rm -f {stripped}; exit $rc"
        )
        result = await self.ssh_client.execute(f"docker exec {layout.container_name} sh -c '{script}'")
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to apply AmneziaWG config: {result.stderr.strip()}")

    async def restore_client(self, name: str, client_config: str) -> bool:
        """Возвращает на сервер peer клиента, у которого уже есть выданный конфиг (его ключи не меняются).

        Нужен sync'у: в БД клиент живой, а на сервере его нет. Идемпотентно: peer уже на месте — False.
        Вызывающий обязан держать ServerLock (read-modify-write awg0.conf).
        """
        validate_name(name)
        params = parse_client_config(client_config)
        await self._require_own_container()
        server_config = await self.ssh_client.read_file(self.layout.host_config_file)
        peers = list_peers(server_config)
        if any(peer.public_key == params.public_key for peer in peers):
            return False
        if params.address in used_peer_addresses(server_config):
            raise CommandExecutionError(f"Address {params.address} is already taken by another peer")

        updated = append_peer(server_config, render_peer_block(name, params.public_key, params.psk, params.address))
        await self.ssh_client.write_file(self.layout.host_config_file, updated)
        await self._apply_config()
        return True

    async def list_clients(self) -> list[VPNClientInfo]:
        """Peer'ы из awg0.conf (источник правды о том, кого сервер пустит) + время последнего рукопожатия."""
        if await self._is_foreign() or not await self._config_exists():
            return []
        layout = self.layout
        peers = list_peers(await self.ssh_client.read_file(layout.host_config_file))
        handshakes: dict[str, datetime | None] = {}
        result = await self.ssh_client.execute(
            f"docker exec {layout.container_name} awg show {layout.interface_name} latest-handshakes"
        )
        if result.exit_status == 0:
            handshakes = parse_latest_handshakes(result.stdout)
        return [
            VPNClientInfo(
                name=peer.name,
                public_key=peer.public_key,
                address=peer.address,
                latest_handshake=handshakes.get(peer.public_key),
            )
            for peer in peers
        ]

    async def sync(self) -> SyncResult:
        """Снимок реального состояния протокола на сервере. Ничего не меняет — сверку с БД делает сервис."""
        status = await self.get_status()
        return SyncResult(provider_status=status, client_list=await self.list_clients())

    async def health_check(self) -> ProviderHealth:
        """Проверки протокола на сервере: контейнер, перезапуск, конфиг, интерфейс, порт, NAT, клиенты. Не меняет."""
        layout = self.layout
        checks: list[HealthCheck] = []
        if await self._is_foreign():
            checks.append(HealthCheck("container", False, f"{layout.container_name} belongs to another application"))
            return ProviderHealth(installed=False, checks=checks, port=None, clients_total=0, clients_active=0)
        state = await self.ssh_client.execute(self._ps("State"))
        raw_state = state.stdout.strip()
        running = raw_state == "running"
        checks.append(HealthCheck("container", running, raw_state or "container not found"))
        policy = await self._restart_policy() if raw_state else None

        port: int | None = None
        config_exists = await self._config_exists()
        installed = bool(raw_state) or config_exists
        if config_exists:
            try:
                port = int(interface_values(await self.ssh_client.read_file(layout.host_config_file))["ListenPort"])
                checks.append(HealthCheck("config", True, f"ListenPort {port}"))
            except (KeyError, ValueError):
                checks.append(HealthCheck("config", False, "ListenPort is missing or invalid"))
        else:
            checks.append(HealthCheck("config", False, f"{layout.host_config_file} not found"))

        if policy is not None:
            checks.append(
                HealthCheck(
                    "restart_policy",
                    policy in RESTARTING_POLICIES,
                    policy if policy in RESTARTING_POLICIES else f"{policy or 'no'}: not restarted after reboot",
                )
            )

        if not running:
            return ProviderHealth(installed=installed, checks=checks, port=port, clients_total=0, clients_active=0)

        listen = await self.ssh_client.execute(
            f"docker exec {layout.container_name} awg show {layout.interface_name} listen-port"
        )
        interface_up = listen.exit_status == 0
        checks.append(
            HealthCheck(
                "interface", interface_up, f"{layout.interface_name} is up" if interface_up else "interface is down"
            )
        )
        if interface_up and port is not None:
            listen_port = listen.stdout.strip()
            checks.append(HealthCheck("listen_port", listen_port == str(port), f"interface listens on {listen_port}"))
        if port is not None:
            # port — число из конфига, а не пользовательский ввод: подставлять в команду безопасно.
            published = await self.ssh_client.execute(f"docker port {layout.container_name} {port}/udp")
            mapping = published.stdout.strip()
            checks.append(
                HealthCheck("port_published", published.exit_status == 0 and bool(mapping), mapping or "not published")
            )
        nat = await self.ssh_client.execute(f"docker exec {layout.container_name} iptables -t nat -C {layout.nat_rule}")
        checks.append(
            HealthCheck("nat", nat.exit_status == 0, "masquerade rule present" if nat.exit_status == 0 else "no rule")
        )

        clients = await self.list_clients()
        now = datetime.now(timezone.utc)
        active = sum(
            1
            for client in clients
            if client.latest_handshake
            and (now - client.latest_handshake).total_seconds() <= ACTIVE_HANDSHAKE_WINDOW_SECONDS
        )
        return ProviderHealth(
            installed=installed, checks=checks, port=port, clients_total=len(clients), clients_active=active
        )

    async def _config_exists(self) -> bool:
        result = await self.ssh_client.execute(f"test -f {self.layout.host_config_file}")
        return result.exit_status == 0

    async def _run_container(self, port: int) -> None:
        layout = self.layout
        await self._require_own_container()
        container_state = await self.ssh_client.execute(self._ps("State"))

        if not container_state.stdout:
            container_run = await self.ssh_client.execute(
                f"docker run -d --name {layout.container_name} --restart {RESTART_POLICY} \
                  --cap-add=NET_ADMIN --device=/dev/net/tun \
                  -p {port}:{port}/udp \
                  -v {layout.host_config_dir}:{layout.container_config_dir} \
                  {AMNEZIAWG_IMAGE} \
                  sh -c 'awg-quick up {layout.container_config_file} && tail -f /dev/null'",
                timeout=300,
            )
            if container_run.exit_status != 0:
                raise DockerError(f"Failed to start container. Error: {container_run.stderr}")
        elif container_state.stdout.strip() == "exited":
            container_start = await self.ssh_client.execute(f"docker start {layout.container_name}")
            if container_start.exit_status != 0:
                raise DockerError(f"Failed to start container. Error: {container_start.stderr}")

        await asyncio.sleep(5)
        container_state = await self.ssh_client.execute(self._ps("State"))
        if container_state.stdout.strip() != "running":
            raise DockerError("Failed to start container.")

    async def _rm_container(self) -> None:
        layout = self.layout
        await self._require_own_container()
        container_state = await self.ssh_client.execute(self._ps("State"))
        if container_state.stdout:
            container_stop = await self.ssh_client.execute(
                f"docker stop {layout.container_name} && docker rm {layout.container_name}"
            )
            rm_config = await self.ssh_client.execute(f"rm -rf {layout.host_config_dir}")

            if container_stop.exit_status != 0:
                raise DockerError(f"Failed to stop container. Error: {container_stop.stderr}")
            if rm_config.exit_status != 0:
                raise OSError(f"Error deleting directory: {rm_config.stderr} (Code: {rm_config.exit_status})")

            container_state = await self.ssh_client.execute(self._ps("State"))

            if container_state.stdout:
                raise DockerError("Failed to remove container.")
