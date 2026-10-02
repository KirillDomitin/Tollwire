#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import re

from core.ssh_exception import CommandExecutionError, DockerError, InstallError, InvalidServerConfigurationError
from infrastructure.ssh.client import SSHClient
from infrastructure.vpn.awg_config import (
    append_peer,
    ensure_nat_hooks,
    generate_secret_keys,
    is_valid_public_key,
    pick_free_address,
    pick_udp_port,
    remove_peer,
    render_awg_config,
    render_client_config,
    render_peer_block,
)
from infrastructure.vpn.config import (
    CONFIG_FILE_NAME,
    CONTAINER_CONFIG_DIR,
    CONTAINER_NAME,
    HOST_CONFIG_DIR,
    INTERFACE_NAME,
    NAT_POST_UP,
)
from infrastructure.vpn.provider import (
    ProviderState,
    ProviderStatus,
    SyncResult,
    VPNClientConfig,
    VPNClientInfo,
    VPNProvider,
)

_ALLOWED = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")

CONFIG_PATH = HOST_CONFIG_DIR
HOST_CONFIG_FILE = f"{HOST_CONFIG_DIR}/{CONFIG_FILE_NAME}"
CONTAINER_CONFIG_FILE = f"{CONTAINER_CONFIG_DIR}/{CONFIG_FILE_NAME}"


def validate_name(name: str) -> None:
    if not _ALLOWED.match(name):
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
                print(f"Warning: Docker started, but failed to enable autostart: {result.stderr.strip()}")

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
            "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.State}}'"
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
            "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.Status}}'"
        )
        version = await self.ssh_client.execute(
            "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.Image}}'"
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

    async def list_clients(self) -> list[VPNClientInfo]:
        raise NotImplementedError("AmneziaWG client list is not implemented yet")

    async def sync(self) -> SyncResult:
        raise NotImplementedError("AmneziaWG sync is not implemented yet")

    async def _run_container(self, port: int) -> None:
        container_state = await self.ssh_client.execute(
            "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.State}}'"
        )

        if not container_state.stdout:
            container_run = await self.ssh_client.execute(
                f"docker run -d --name {CONTAINER_NAME} \
                  --cap-add=NET_ADMIN --device=/dev/net/tun \
                  -p {port}:{port}/udp \
                  -v {CONFIG_PATH}:/etc/amnezia/awg \
                  amneziavpn/amneziawg-go:latest \
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
            "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.State}}'"
        )
        if container_state.stdout.strip() != "running":
            raise DockerError("Failed to start container.")

    async def _rm_container(self) -> None:
        container_state = await self.ssh_client.execute(
            "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.State}}'"
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
                "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.State}}'"
            )

            if container_state.stdout:
                raise DockerError("Failed to remove container.")
