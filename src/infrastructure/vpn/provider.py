#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from infrastructure.ssh.client import SSHClient


class ProviderState(enum.Enum):
    NOT_INSTALLED = "not_installed"
    STOPPED = "stopped"
    RUNNING = "running"


@dataclass
class ProviderStatus:
    server_status: ProviderState
    version: str | None
    port: int | None
    uptime: str | None


@dataclass
class VPNClientInfo:
    name: str
    created_at: datetime | None
    last_activity: datetime | None


@dataclass
class VPNClientConfig:
    name: str
    config: str = field(repr=False)


@dataclass
class SyncResult:
    provider_status: ProviderStatus
    client_list: list[VPNClientInfo]


class VPNProvider(ABC):
    def __init__(self, ssh_client: SSHClient):
        self.ssh_client = ssh_client

    @abstractmethod
    async def install(self) -> ProviderStatus: ...
    @abstractmethod
    async def uninstall(self) -> ProviderStatus: ...
    @abstractmethod
    async def get_status(self) -> ProviderStatus: ...
    @abstractmethod
    async def create_client(self, name: str) -> VPNClientConfig: ...
    @abstractmethod
    async def revoke_client(self, name: str) -> None: ...
    @abstractmethod
    async def list_clients(self) -> list[VPNClientInfo]: ...
    @abstractmethod
    async def sync(self) -> SyncResult: ...
