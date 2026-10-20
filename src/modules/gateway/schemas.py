#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from modules.gateway.models.gateway_assignment import AssignReason
from modules.gateway.models.gateway_exit import MAX_WEIGHT, ExitState

MAX_CLIENTS_LIMIT = 100_000


class GatewayExitCreate(BaseModel):
    """Тело POST /servers/{id}/gateway/exits: какой сервер подключить выходом."""

    exit_server_id: UUID
    weight: int = Field(default=1, ge=1, le=MAX_WEIGHT)
    max_clients: int | None = Field(default=None, ge=1, le=MAX_CLIENTS_LIMIT)


class GatewayExitUpdate(BaseModel):
    """Тело PATCH: только поля, переданные в запросе (max_clients: null снимает предел)."""

    weight: int | None = Field(default=None, ge=1, le=MAX_WEIGHT)
    max_clients: int | None = Field(default=None, ge=1, le=MAX_CLIENTS_LIMIT)


class GatewayExitRead(BaseModel):
    """Выход шлюза для API. Ключей и конфига внутреннего клиента здесь нет и быть не должно."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exit_server_id: UUID
    exit_server_name: str
    slot: int
    state: ExitState
    health: str | None
    last_handshake_at: datetime | None
    checked_at: datetime | None
    weight: int
    max_clients: int | None
    assigned_clients: int  # сколько живых клиентов шлюза идёт через этот выход


class GatewayAssignmentRead(BaseModel):
    """Назначение выхода клиенту шлюза. Без адреса и ключей клиента: клиент определяется по credential_id."""

    credential_id: UUID
    exit_id: UUID
    slot: int
    reason: AssignReason
    assigned_at: datetime
