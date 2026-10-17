#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from core.schemas import PatchModel
from modules.access.models import AccessGrantStatus, CredentialSource


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
    source: CredentialSource
    external_name: str | None = None  # имя клиента в AmneziaVPN (у импортированного)


class VPNCredentialConfigRead(BaseModel):
    id: UUID
    access_grant_id: UUID
    config: str
    # Тот же конфиг в формате приложения протокола (AmneziaVPN: vpn://) с именем сервера; None — формата нет.
    share_link: str | None = None
    # Он же серией QR-кодов для сканера приложения (показывать по очереди); None — хватает одного QR.
    share_qr: list[str] | None = None


class AccessGrantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    subscription_id: UUID
    server_id: UUID
    protocol_id: UUID
    status: AccessGrantStatus
    revoked_at: datetime | None
    created_at: datetime
