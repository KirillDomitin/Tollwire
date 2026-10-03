#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from modules.vpn_users.models.user import VPNUserStatus


# Email — идентификатор пользователя (будущая регистрация), поэтому на входе он проверяется (EmailStr).
# В Read — обычная строка: ответ не должен падать на старых записях, сохранённых до проверки.
class VPNUserCreate(BaseModel):
    username: str
    email: EmailStr
    description: str | None = None


class VPNUserUpdate(BaseModel):
    username: str | None = None
    email: EmailStr | None = None
    description: str | None = None


class VPNUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    username: str
    email: str
    status: VPNUserStatus
    description: str | None = None
    created_at: datetime
