#!/usr/bin/python
# -*- coding: utf-8 -*-
import re

from core.ssh_exception import CommandExecutionError, DockerError, InstallError, InvalidServerConfigurationError
from infrastructure.ssh.client import SSHClient
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

    async def _run_container(self) -> None:
        container_state = await self.ssh_client.execute(
            "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.State}}'"
        )
        if not container_state.stdout:
            container_run = await self.ssh_client.execute(
                f"docker run -d --name {CONTAINER_NAME} nginx:alpine", timeout=300
            )
            if container_run.exit_status != 0:
                raise DockerError(f"Failed to start container. Error: {container_run.stderr}")
        elif container_state.stdout.strip() == "exited":
            container_start = await self.ssh_client.execute(f"docker start {CONTAINER_NAME}")
            if container_start.exit_status != 0:
                raise DockerError(f"Failed to start container. Error: {container_start.stderr}")

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
            if container_stop.exit_status != 0:
                raise DockerError(f"Failed to stop container. Error: {container_stop.stderr}")
            container_state = await self.ssh_client.execute(
                "docker ps -a --filter name=" + CONTAINER_NAME + " --format '{{.State}}'"
            )
            if container_state.stdout:
                raise DockerError("Failed to remove container.")

    async def install(self) -> ProviderStatus:
        await self._validate_server()
        await self._ensure_docker()
        await self._run_container()
        return ProviderStatus(
            server_status=ProviderState.RUNNING, version=None, protocols=[]
        )  # TODO подставлять версию амнезии

    async def uninstall(self) -> ProviderStatus:
        await self._rm_container()
        return ProviderStatus(server_status=ProviderState.NOT_INSTALLED, version=None, protocols=[])

    async def get_status(self) -> ProviderStatus:
        raise NotImplementedError("Dummy status")


class AmneziaWGProvider(VPNProvider):
    def __init__(self, ssh_client: SSHClient):
        super().__init__(ssh_client)
        self.ssh_client = ssh_client
        self.installer = AmneziaInstaller(ssh_client)

    async def install(self) -> ProviderStatus:
        return await self.installer.install()

    async def uninstall(self) -> ProviderStatus:
        return await self.installer.uninstall()

    async def get_status(self) -> ProviderStatus:
        return await self.installer.get_status()

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
