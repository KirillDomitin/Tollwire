#!/usr/bin/python
# -*- coding: utf-8 -*-
import unicodedata
from datetime import datetime, timezone
from typing import Annotated, Self
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from core.schemas import PatchModel
from infrastructure.vpn.awg_config import is_valid_public_key
from modules.protocols.models.protocol import APP_PROTOCOLS, ProtocolType
from modules.servers.models.credential import AuthType
from modules.servers.models.server import HealthStatus, ServerStatus
from modules.vpn_users.schemas import Email

# Имя хоста или IP (v4/v6): буквы, цифры, точка, дефис, двоеточие. Без пробелов и спецсимволов — адрес
# уходит в SSH-подключение, логи и аудит.
HOST_PATTERN = r"^[A-Za-z0-9.:-]+$"
Name = Annotated[str, Field(min_length=1, max_length=100)]
Host = Annotated[str, Field(min_length=1, max_length=100, pattern=HOST_PATTERN)]
Port = Annotated[int, Field(ge=1, le=65535)]
Description = Annotated[str, Field(max_length=300)]
# Имя пользователя SSH: только безопасные символы — оно уходит в подключение, логи и аудит.
Username = Annotated[str, Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9._-]+$")]
Secret = Annotated[str, Field(min_length=1, max_length=16384)]  # пустой секрет SSHClient всё равно не примет


class ServerCreate(BaseModel):
    name: Name
    host: Host
    ssh_port: Port = 22
    description: Description | None = None


class ServerUpdate(PatchModel):
    NULLABLE = frozenset({"description"})
    name: Name | None = None
    host: Host | None = None
    ssh_port: Port | None = None
    description: Description | None = None


class ServerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    host: str
    ssh_port: int
    status: ServerStatus
    description: str | None = None
    health_status: HealthStatus | None = None
    last_health_at: datetime | None = None
    ssh_host_key_fingerprint: str | None = None  # None — ключ ещё не закреплён
    created_at: datetime


class CredentialCreate(BaseModel):
    auth_type: AuthType = AuthType.PASSWORD
    username: Username = "root"
    secret: Secret


class CredentialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    server_id: UUID
    auth_type: AuthType
    username: str
    managed_key: bool  # ключ сгенерирован и поставлен панелью
    key_fingerprint: str | None = None  # отпечаток ключа входа (None — вход по паролю)
    created_at: datetime


class SshKeyInstall(BaseModel):
    """Тело POST /ssh-key. Пусто — ключ ставится по текущей учётке (или меняется на новый); логин и пароль —
    сначала сохранить их как учётку («пароль один раз»). Только оба поля или ни одного."""

    username: Username | None = None
    password: Secret | None = None

    @model_validator(mode="after")
    def both_or_none(self) -> "SshKeyInstall":
        if (self.username is None) != (self.password is None):
            raise ValueError("Provide both username and password, or neither")
        return self


class HostKeyPin(BaseModel):
    # Строка из /etc/ssh/ssh_host_ed25519_key.pub (или public_key из ответа scan). Комментарий допускается.
    public_key: str = Field(min_length=16, max_length=4096)


class HostKeyScan(BaseModel):
    """Ключ, который сервер предъявил сейчас (без входа). Сверить fingerprint с консолью хостинга перед PUT."""

    public_key: str
    fingerprint: str
    pinned_fingerprint: str | None
    matches_pinned: bool | None  # None — закреплённого ключа нет


class TestConnectionResult(BaseModel):
    success: bool
    message: str


class DiscoveredClientRead(BaseModel):
    public_key: str
    address: str | None
    name: str | None
    latest_handshake: datetime | None
    supported: bool
    status: str


class DiscoveredProtocolRead(BaseModel):
    type: str
    container: str
    state: str
    image: str | None
    port: int | None
    clients: list[DiscoveredClientRead]


class DiscoveryRead(BaseModel):
    checked_at: datetime | None
    protocols: list[DiscoveredProtocolRead]


def _public_key(value: str) -> str:
    if not is_valid_public_key(value):
        raise ValueError("Not a WireGuard public key")
    return value


PublicKey = Annotated[str, AfterValidator(_public_key)]
IMPORT_MAX_CLIENTS = 250


class ImportClient(BaseModel):
    """Один клиент установки AmneziaVPN для переноса в панель. Пользователь: существующий (user_id) или новый
    (username и, если известен, email). PSK и прочие секреты в запросе не принимаются и в параметры задачи не идут."""

    public_key: PublicKey
    username: Name
    email: Email | None = None
    user_id: UUID | None = None

    @field_validator("username")
    @classmethod
    def _printable_username(cls, value: str) -> str:
        """Имя уходит в БД и в параметры задачи (JSONB): управляющие символы (в том числе NUL) недопустимы."""
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
            raise ValueError("Username must not contain control characters")
        return value

    @model_validator(mode="after")
    def _one_user_source(self) -> Self:
        if self.user_id is not None and self.email is not None:
            raise ValueError("Provide either user_id or email, not both")
        return self


class ClientImportParams(BaseModel):
    """Параметры задачи IMPORT_CLIENTS (Job.params). Воркер разбирает ими сохранённый JSON, поэтому срок в
    будущем здесь не проверяется: к моменту выполнения он мог пройти."""

    protocol_type: ProtocolType
    valid_until: AwareDatetime
    clients: Annotated[list[ImportClient], Field(min_length=1, max_length=IMPORT_MAX_CLIENTS)]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.protocol_type not in APP_PROTOCOLS:
            raise ValueError("Only AmneziaVPN application protocols can be imported")
        keys = [client.public_key for client in self.clients]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate public_key in clients")
        return self


class ClientImportCreate(ClientImportParams):
    """Тело POST /servers/{id}/import."""

    @model_validator(mode="after")
    def _future(self) -> Self:
        if self.valid_until <= datetime.now(timezone.utc):
            raise ValueError("valid_until must be in the future")
        return self
