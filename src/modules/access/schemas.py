#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from modules.access.models import AccessGrantStatus


class SubscriptionCreate(BaseModel):
    valid_from: datetime
    valid_until: datetime


class SubscriptionUpdate(BaseModel):
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class SubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    user_id: UUID
    status: AccessGrantStatus
    valid_from: datetime
    valid_until: datetime
    revoked_at: datetime | None
    created_at: datetime


class AccessGrantCreate(BaseModel):
    protocol_id: UUID


class VPNCredentialRead(BaseModel):
    """Метаданные credential. Конфига здесь нет намеренно — его отдаёт только отдельный эндпоинт."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    access_grant_id: UUID
    revoked_at: datetime | None
    created_at: datetime


class VPNCredentialConfigRead(BaseModel):
    id: UUID
    access_grant_id: UUID
    config: str


class AccessGrantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    subscription_id: UUID
    server_id: UUID
    protocol_id: UUID
    status: AccessGrantStatus
    revoked_at: datetime | None
    created_at: datetime
