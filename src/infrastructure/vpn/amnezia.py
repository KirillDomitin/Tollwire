#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import logging
import re
from datetime import datetime, timezone

from core.ssh_exception import CommandExecutionError, DockerError, InstallError, InvalidServerConfigurationError
from infrastructure.ssh.client import SSHClient
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
    CONFIG_FILE_NAME,
    CONTAINER_CONFIG_DIR,
    CONTAINER_NAME,
    CONTAINER_NAME_FILTER,
    HOST_CONFIG_DIR,
    INTERFACE_NAME,
    INTERNET_PROBE_COMMAND,
    NAT_POST_UP,
    NAT_RULE,
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

CONFIG_PATH = HOST_CONFIG_DIR
HOST_CONFIG_FILE = f"{HOST_CONFIG_DIR}/{CONFIG_FILE_NAME}"
CONTAINER_CONFIG_FILE = f"{CONTAINER_CONFIG_DIR}/{CONFIG_FILE_NAME}"


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
            install_docker = await self.ssh_client.execute("apt-get update && apt-get install -y docker.io")
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
    def __init__(self, ssh_client: SSHClient):
        super().__init__(ssh_client)
        self.ssh_client = ssh_client
        self.installer = AmneziaInstaller(ssh_client)

    async def install(self) -> ProviderStatus:
        test = await self.ssh_client.execute(f"test -f {CONFIG_PATH}/awg0.conf")
        if test.exit_status == 0:
            # Повторная установка чинит серверы, поставленные до появления NAT-хуков.
            await self._ensure_nat()
            return await self.get_status()
        port = pick_udp_port()
        keys = generate_secret_keys()
        config = render_awg_config(keys.private_key, port)
        await self.ssh_client.write_file(f"{CONFIG_PATH}/awg0.conf", config)
        await self._run_container(port)
        return await self.get_status()

    async def uninstall(self) -> ProviderStatus:
        await self._rm_container()
        return await self.get_status()

    async def get_status(self) -> ProviderStatus:
        status = await self.ssh_client.execute(
            "docker ps -a --filter " + CONTAINER_NAME_FILTER + " --format '{{.State}}'"
        )
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

        port = await self.ssh_client.execute(f"grep 'ListenPort' {CONFIG_PATH}/awg0.conf | cut -d' ' -f3")
        if port.exit_status == 0:
            port_str = port.stdout.strip()
            port_result = int(port_str)

        elif port.exit_status == 1:
            raise CommandExecutionError("Error: 'ListenPort' key not found in the configuration file.")

        elif port.exit_status == 2:
            raise CommandExecutionError(f"Error: Configuration file at {CONFIG_PATH}/awg0.conf does not exist.")

        else:
            raise CommandExecutionError(f"Unexpected error (Code {port.exit_status}): {port.stderr.strip()}")

        uptime = await self.ssh_client.execute(
            "docker ps -a --filter " + CONTAINER_NAME_FILTER + " --format '{{.Status}}'"
        )
        version = await self.ssh_client.execute(
            "docker ps -a --filter " + CONTAINER_NAME_FILTER + " --format '{{.Image}}'"
        )
        if uptime.exit_status == 0 and uptime.stdout.strip():
            uptime_result = uptime.stdout.strip()
        else:
            raise CommandExecutionError(f"Error or Container '{CONTAINER_NAME}' not found while checking uptime.")

        if version.exit_status == 0 and version.stdout.strip():
            version_result = version.stdout.strip()
        else:
            raise CommandExecutionError(f"Error or Container '{CONTAINER_NAME}' not found while checking version.")

        return ProviderStatus(
            server_status=status_result, version=version_result, port=port_result, uptime=uptime_result
        )

    async def create_client(self, name: str) -> VPNClientConfig:
        """Добавляет клиента на сервер и возвращает конфиг для его приложения.

        Делает read-modify-write серверного awg0.conf — вызывающий обязан держать ServerLock.
        """
        validate_name(name)
        server_config = await self.ssh_client.read_file(HOST_CONFIG_FILE)
        address = pick_free_address(server_config)
        keys = generate_secret_keys()
        server_public_key = await self._server_public_key()

        updated = append_peer(server_config, render_peer_block(name, keys.public_key, keys.psk, address))
        await self.ssh_client.write_file(HOST_CONFIG_FILE, updated)
        await self._apply_config()

        client_config = render_client_config(
            server_config=server_config,
            client_private_key=keys.private_key,
            client_address=address,
            psk=keys.psk,
            server_public_key=server_public_key,
            endpoint_host=self.ssh_client.host,
        )
        return VPNClientConfig(name=name, public_key=keys.public_key, address=str(address), config=client_config)

    async def revoke_client(self, public_key: str) -> None:
        """Убирает клиента с сервера. Идемпотентно: отсутствующий клиент — не ошибка.

        Вызывающий обязан держать ServerLock (read-modify-write awg0.conf).
        """
        if not is_valid_public_key(public_key):
            raise ValueError("Invalid WireGuard public key")
        server_config = await self.ssh_client.read_file(HOST_CONFIG_FILE)
        updated, removed = remove_peer(server_config, public_key)
        if not removed:
            return
        await self.ssh_client.write_file(HOST_CONFIG_FILE, updated)
        await self._apply_config()

    async def _ensure_nat(self) -> None:
        """Гарантирует выход клиентов в интернет: хуки NAT в конфиге и правило в работающем контейнере.

        Хуки сработают при следующем старте контейнера; чтобы не перезапускать его (и не рвать
        соединения клиентов), правило дополнительно применяется сразу. Обе операции идемпотентны.
        """
        server_config = await self.ssh_client.read_file(HOST_CONFIG_FILE)
        updated, changed = ensure_nat_hooks(server_config)
        if changed:
            await self.ssh_client.write_file(HOST_CONFIG_FILE, updated)

        running = await self.ssh_client.execute(f"docker inspect -f '{{{{.State.Running}}}}' {CONTAINER_NAME}")
        if running.stdout.strip() != "true":
            return
        result = await self.ssh_client.execute(f"docker exec {CONTAINER_NAME} sh -c '{NAT_POST_UP}'")
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to apply NAT rule: {result.stderr.strip()}")

    async def _server_public_key(self) -> str:
        # Берём публичный ключ у живого интерфейса — приватный ключ сервера не покидает сервер.
        result = await self.ssh_client.execute(f"docker exec {CONTAINER_NAME} awg show {INTERFACE_NAME} public-key")
        public_key = result.stdout.strip()
        if result.exit_status != 0 or not is_valid_public_key(public_key):
            raise CommandExecutionError(f"Failed to read server public key: {result.stderr.strip()}")
        return public_key

    async def _apply_config(self) -> None:
        # awg-quick strip убирает строки, которых не понимает awg (Address, DNS…), а syncconf применяет
        # конфиг к живому интерфейсу без перезапуска: существующие клиенты не отключаются.
        # Временный файл содержит приватный ключ сервера — удаляем при любом исходе.
        stripped = "/tmp/awg0.stripped"
        script = (
            f"awg-quick strip {CONTAINER_CONFIG_FILE} > {stripped}"
            f" && awg syncconf {INTERFACE_NAME} {stripped}; rc=$?; rm -f {stripped}; exit $rc"
        )
        result = await self.ssh_client.execute(f"docker exec {CONTAINER_NAME} sh -c '{script}'")
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to apply AmneziaWG config: {result.stderr.strip()}")

    async def restore_client(self, name: str, client_config: str) -> bool:
        """Возвращает на сервер peer клиента, у которого уже есть выданный конфиг (его ключи не меняются).

        Нужен sync'у: в БД клиент живой, а на сервере его нет. Идемпотентно: peer уже на месте — False.
        Вызывающий обязан держать ServerLock (read-modify-write awg0.conf).
        """
        validate_name(name)
        params = parse_client_config(client_config)
        server_config = await self.ssh_client.read_file(HOST_CONFIG_FILE)
        peers = list_peers(server_config)
        if any(peer.public_key == params.public_key for peer in peers):
            return False
        if params.address in used_peer_addresses(server_config):
            raise CommandExecutionError(f"Address {params.address} is already taken by another peer")

        updated = append_peer(server_config, render_peer_block(name, params.public_key, params.psk, params.address))
        await self.ssh_client.write_file(HOST_CONFIG_FILE, updated)
        await self._apply_config()
        return True

    async def list_clients(self) -> list[VPNClientInfo]:
        """Peer'ы из awg0.conf (источник правды о том, кого сервер пустит) + время последнего рукопожатия."""
        if not await self._config_exists():
            return []
        peers = list_peers(await self.ssh_client.read_file(HOST_CONFIG_FILE))
        handshakes: dict[str, datetime | None] = {}
        result = await self.ssh_client.execute(
            f"docker exec {CONTAINER_NAME} awg show {INTERFACE_NAME} latest-handshakes"
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
        """Проверки протокола на сервере: контейнер, конфиг, интерфейс, порт, NAT, клиенты. Ничего не меняет."""
        checks: list[HealthCheck] = []
        state = await self.ssh_client.execute(
            "docker ps -a --filter " + CONTAINER_NAME_FILTER + " --format '{{.State}}'"
        )
        raw_state = state.stdout.strip()
        running = raw_state == "running"
        checks.append(HealthCheck("container", running, raw_state or "container not found"))

        port: int | None = None
        config_exists = await self._config_exists()
        installed = bool(raw_state) or config_exists
        if config_exists:
            try:
                port = int(interface_values(await self.ssh_client.read_file(HOST_CONFIG_FILE))["ListenPort"])
                checks.append(HealthCheck("config", True, f"ListenPort {port}"))
            except (KeyError, ValueError):
                checks.append(HealthCheck("config", False, "ListenPort is missing or invalid"))
        else:
            checks.append(HealthCheck("config", False, f"{HOST_CONFIG_FILE} not found"))

        if not running:
            return ProviderHealth(installed=installed, checks=checks, port=port, clients_total=0, clients_active=0)

        listen = await self.ssh_client.execute(f"docker exec {CONTAINER_NAME} awg show {INTERFACE_NAME} listen-port")
        interface_up = listen.exit_status == 0
        checks.append(
            HealthCheck("interface", interface_up, f"{INTERFACE_NAME} is up" if interface_up else "interface is down")
        )
        if interface_up and port is not None:
            listen_port = listen.stdout.strip()
            checks.append(HealthCheck("listen_port", listen_port == str(port), f"interface listens on {listen_port}"))
        if port is not None:
            # port — число из конфига, а не пользовательский ввод: подставлять в команду безопасно.
            published = await self.ssh_client.execute(f"docker port {CONTAINER_NAME} {port}/udp")
            mapping = published.stdout.strip()
            checks.append(
                HealthCheck("port_published", published.exit_status == 0 and bool(mapping), mapping or "not published")
            )
        nat = await self.ssh_client.execute(f"docker exec {CONTAINER_NAME} iptables -t nat -C {NAT_RULE}")
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
        result = await self.ssh_client.execute(f"test -f {HOST_CONFIG_FILE}")
        return result.exit_status == 0

    async def _run_container(self, port: int) -> None:
        container_state = await self.ssh_client.execute(
            "docker ps -a --filter " + CONTAINER_NAME_FILTER + " --format '{{.State}}'"
        )

        if not container_state.stdout:
            container_run = await self.ssh_client.execute(
                f"docker run -d --name {CONTAINER_NAME} \
                  --cap-add=NET_ADMIN --device=/dev/net/tun \
                  -p {port}:{port}/udp \
                  -v {CONFIG_PATH}:/etc/amnezia/awg \
                  {AMNEZIAWG_IMAGE} \
                  sh -c 'awg-quick up /etc/amnezia/awg/awg0.conf && tail -f /dev/null'",
                timeout=300,
            )
            if container_run.exit_status != 0:
                raise DockerError(f"Failed to start container. Error: {container_run.stderr}")
        elif container_state.stdout.strip() == "exited":
            container_start = await self.ssh_client.execute(f"docker start {CONTAINER_NAME}")
            if container_start.exit_status != 0:
                raise DockerError(f"Failed to start container. Error: {container_start.stderr}")

        await asyncio.sleep(5)
        container_state = await self.ssh_client.execute(
            "docker ps -a --filter " + CONTAINER_NAME_FILTER + " --format '{{.State}}'"
        )
        if container_state.stdout.strip() != "running":
            raise DockerError("Failed to start container.")

    async def _rm_container(self) -> None:
        container_state = await self.ssh_client.execute(
            "docker ps -a --filter " + CONTAINER_NAME_FILTER + " --format '{{.State}}'"
        )
        if container_state.stdout:
            container_stop = await self.ssh_client.execute(
                f"docker stop {CONTAINER_NAME} && docker rm {CONTAINER_NAME}"
            )
            rm_config = await self.ssh_client.execute(f"rm -rf {CONFIG_PATH}")

            if container_stop.exit_status != 0:
                raise DockerError(f"Failed to stop container. Error: {container_stop.stderr}")
            if rm_config.exit_status != 0:
                raise OSError(f"Error deleting directory: {rm_config.stderr} (Code: {rm_config.exit_status})")

            container_state = await self.ssh_client.execute(
                "docker ps -a --filter " + CONTAINER_NAME_FILTER + " --format '{{.State}}'"
            )

            if container_state.stdout:
                raise DockerError("Failed to remove container.")
