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
    """Клиент, как его видит сервер. Секретов нет: публичный ключ и адрес можно показывать."""

    name: str | None
    public_key: str
    address: str | None
    latest_handshake: datetime | None


@dataclass
class VPNClientConfig:
    name: str
    public_key: str
    address: str
    config: str = field(repr=False)


@dataclass
class HealthCheck:
    """Одна проверка health: имя, прошла ли, пояснение для человека (без секретов)."""

    name: str
    ok: bool
    detail: str | None = None


@dataclass
class ProviderHealth:
    installed: bool  # на сервере есть следы протокола (контейнер или конфиг)
    checks: list[HealthCheck]
    port: int | None
    clients_total: int
    clients_active: int


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
    async def revoke_client(self, public_key: str) -> None: ...
    @abstractmethod
    async def restore_client(self, name: str, client_config: str) -> bool: ...
    @abstractmethod
    async def list_clients(self) -> list[VPNClientInfo]: ...
    @abstractmethod
    async def sync(self) -> SyncResult: ...
    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...
