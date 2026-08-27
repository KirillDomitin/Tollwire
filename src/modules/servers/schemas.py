#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from modules.servers.models.credential import AuthType
from modules.servers.models.server import ServerStatus


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


class TestConnectionResult(BaseModel):
    success: bool
    message: str
