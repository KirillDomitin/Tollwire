#!/usr/bin/python
# -*- coding: utf-8 -*-
import re

from infrastructure.ssh.client import SSHClient
from infrastructure.vpn.provider import ProviderStatus, SyncResult, VPNClientConfig, VPNClientInfo, VPNProvider

_ALLOWED = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


class AmneziaInstaller:
    def __init__(self, ssh_client: SSHClient):
        self.ssh_client = ssh_client

    async def install(self) -> ProviderStatus:
        raise NotImplementedError("Dummy install")

    async def uninstall(self) -> ProviderStatus:
        raise NotImplementedError("Dummy uninstall")

    async def get_status(self) -> ProviderStatus:
        raise NotImplementedError("Dummy status")


class AmneziaWGProvider(VPNProvider):
    def __init__(self, ssh_client: SSHClient):
        super().__init__(ssh_client)
        self.installer = AmneziaInstaller(ssh_client)

    async def install(self) -> ProviderStatus:
        return await self.installer.install()

    async def uninstall(self) -> ProviderStatus:
        return await self.installer.uninstall()

    async def get_status(self) -> ProviderStatus:
        return await self.installer.get_status()

    async def create_client(self, name: str) -> VPNClientConfig:
        if not _ALLOWED.match(name):
            raise ValueError("Invalid name format")
        raise NotImplementedError("AmneziaWG client creation is not implemented yet")

    async def revoke_client(self, name: str) -> None:
        if not _ALLOWED.match(name):
            raise ValueError(
                "Name must be 1-32 characters long and contain only letters, numbers, underscores, or hyphens"
            )
        raise NotImplementedError("AmneziaWG client revoke is not implemented yet")

    async def list_clients(self) -> list[VPNClientInfo]:
        raise NotImplementedError("AmneziaWG client list is not implemented yet")

    async def sync(self) -> SyncResult:
        raise NotImplementedError("AmneziaWG sync is not implemented yet")
