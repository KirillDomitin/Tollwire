#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from core.schemas import PatchModel
from modules.access.models import AccessGrantStatus


# AwareDatetime — только с часовым поясом: «2026-10-01T00:00» без пояса непонятно чьё время, а сравнение
# с datetime.now(timezone.utc) упало бы. Порядок дат при PATCH проверяет сервис (там видны обе).
class SubscriptionCreate(BaseModel):
    valid_from: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def _period(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self


class SubscriptionUpdate(PatchModel):
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None


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
