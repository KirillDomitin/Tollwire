#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from core.schemas import PatchModel
from modules.access.models import AccessGrantStatus
from modules.vpn_users.models.user import VPNUserStatus

# Email — идентификатор пользователя (будущая регистрация), поэтому на входе он проверяется (EmailStr).
# В Read — обычная строка: ответ не должен падать на старых записях, сохранённых до проверки.
Username = Annotated[str, Field(min_length=1, max_length=100)]
Email = Annotated[EmailStr, Field(max_length=100)]
Description = Annotated[str, Field(max_length=300)]


class VPNUserCreate(BaseModel):
    username: Username
    email: Email
    description: Description | None = None


class VPNUserUpdate(PatchModel):
    NULLABLE = frozenset({"description"})
    username: Username | None = None
    email: Email | None = None
    description: Description | None = None


class VPNUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    username: str
    email: str | None  # None — пользователь создан импортом клиентов из AmneziaVPN
    status: VPNUserStatus
    description: str | None = None
    created_at: datetime
    # Заполнены у пришедших через Telegram-бота (и привязанных вручную); bot_blocked_at - человек заблокировал бота.
    telegram_id: int | None = None
    telegram_username: str | None = None
    bot_blocked_at: datetime | None = None


class AccessSummary(BaseModel):
    """Последняя подписка пользователя — чтобы в списке было видно, у кого доступ и до какого числа."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: AccessGrantStatus
    valid_from: datetime
    valid_until: datetime


class VPNUserListItem(VPNUserRead):
    access: AccessSummary | None = None  # None — подписок ещё не было
