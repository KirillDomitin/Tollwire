#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from core.ssh_exception import UnsupportedOperationError
from infrastructure.ssh.client import SSHClient
from infrastructure.vpn.config import ACTIVE_HANDSHAKE_WINDOW_SECONDS


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


@dataclass(frozen=True)
class DiscoveredClient:
    public_key: str
    address: str | None
    name: str | None
    latest_handshake: datetime | None
    supported: bool


@dataclass(frozen=True)
class ImportablePeer:
    """Клиент чужой установки, которого можно перенести в БД панели. PSK — секрет: не в repr, не в логи и исключения."""

    public_key: str
    address: str | None  # IPv4 без маски; None, если peer панель не поддерживает (supported=False)
    psk: str | None = field(repr=False)  # None — у peer'а PresharedKey нет
    name: str | None
    supported: bool


@dataclass
class Discovery:
    container: str
    state: ProviderState
    image: str | None
    port: int | None
    clients: list[DiscoveredClient]


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
    async def restore_peer(self, name: str, public_key: str, psk: str, address: str) -> bool:
        """Возвращает на сервер peer клиента, у которого нет конфига с приватным ключом (подхваченного из чужой
        установки). Нужен sync'у. True — peer добавлен, False — уже был. По умолчанию не поддерживается."""
        raise UnsupportedOperationError(f"{type(self).__name__} cannot restore a peer without a client config")

    @abstractmethod
    async def list_clients(self) -> list[VPNClientInfo]: ...
    @abstractmethod
    async def sync(self) -> SyncResult: ...
    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...

    async def discover(self) -> Discovery | None:
        """Что провайдер видит на сервере в режиме «только чтение». None — к поиску не относится."""
        return None

    async def read_importable(self) -> list[ImportablePeer]:
        """Клиенты установки, которых можно импортировать в панель (только чтение). Не поддержано по умолчанию;
        контейнер не готов или конфиг нечитаем — CommandExecutionError."""
        raise UnsupportedOperationError(f"{type(self).__name__} does not support importing clients")

    async def client_counts(self, now: datetime) -> tuple[int, int]:
        """Клиентов на сервере всего и онлайн. «Онлайн» — было рукопожатие за последние
        ACTIVE_HANDSHAKE_WINDOW_SECONDS: при живом трафике WireGuard повторяет его примерно раз в 2 минуты."""
        clients = await self.list_clients()
        online = sum(
            1
            for client in clients
            if client.latest_handshake
            and (now - client.latest_handshake).total_seconds() <= ACTIVE_HANDSHAKE_WINDOW_SECONDS
        )
        return len(clients), online

    @classmethod
    def share_link(cls, client_config: str, display_name: str) -> str | None:
        """Конфиг клиента в формате «своего» приложения протокола (импорт одной строкой/QR) с именем сервера.
        Без SSH — только из готового конфига. None: у протокола такого формата нет, клиенту отдаётся сам конфиг."""
        return None

    @classmethod
    def share_qr_codes(cls, client_config: str, display_name: str) -> list[str] | None:
        """Тот же конфиг серией QR-кодов в формате сканера приложения. None — сканер читает обычный QR с конфигом."""
        return None
