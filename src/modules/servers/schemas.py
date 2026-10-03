#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from modules.servers.models.credential import AuthType
from modules.servers.models.server import HealthStatus, ServerStatus


class ServerCreate(BaseModel):
    name: str
    host: str
    ssh_port: int = 22
    description: str | None = None


class ServerUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    ssh_port: int | None = None
    description: str | None = None


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
    username: str = "root"
    secret: str


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
