#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import re

from core.ssh_exception import CommandExecutionError, DockerError, InstallError, InvalidServerConfigurationError
from infrastructure.ssh.client import SSHClient
from infrastructure.vpn.awg_config import generate_secret_keys, pick_udp_port, render_awg_config
from infrastructure.vpn.config import CONTAINER_NAME
from infrastructure.vpn.provider import (
    ProviderState,
    ProviderStatus,
    SyncResult,
    VPNClientConfig,
    VPNClientInfo,
    VPNProvider,
)

_ALLOWED = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")

CONFIG_PATH = "/opt/vpn-admin/awg"


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
            return ProviderStatus(
                server_status=status_result, version=None, port=None, uptime=None
            )

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
        validate_name(name)
        raise NotImplementedError("AmneziaWG client creation is not implemented yet")

    async def revoke_client(self, name: str) -> None:
        validate_name(name)
        raise NotImplementedError("AmneziaWG client revoke is not implemented yet")

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
