#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.schemas import PatchModel
from modules.servers.models.credential import AuthType
from modules.servers.models.server import HealthStatus, ServerStatus

# Имя хоста или IP (v4/v6): буквы, цифры, точка, дефис, двоеточие. Без пробелов и спецсимволов — адрес
# уходит в SSH-подключение, логи и аудит.
HOST_PATTERN = r"^[A-Za-z0-9.:-]+$"
Name = Annotated[str, Field(min_length=1, max_length=100)]
Host = Annotated[str, Field(min_length=1, max_length=100, pattern=HOST_PATTERN)]
Port = Annotated[int, Field(ge=1, le=65535)]
Description = Annotated[str, Field(max_length=300)]


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
    username: str = Field(default="root", min_length=1, max_length=50, pattern=r"^[A-Za-z0-9._-]+$")
    secret: str = Field(min_length=1, max_length=16384)  # пустой секрет SSHClient всё равно не примет


class CredentialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    server_id: UUID
    auth_type: AuthType
    username: str
    created_at: datetime


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
