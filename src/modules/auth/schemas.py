#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from modules.auth.models.admin import AdminRole
from modules.auth.permissions import ROLE_PERMISSIONS

MIN_PASSWORD_LENGTH = 12


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class AdminRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: EmailStr
    is_active: bool
    role: AdminRole
    last_login_at: datetime | None = None
    must_change_password: bool = False
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_superuser(self) -> bool:  # поле из ТЗ (§6): производное от роли
        return self.role == AdminRole.SUPERADMIN

    @computed_field  # type: ignore[prop-decorator]
    @property
    def permissions(self) -> list[str]:  # чтобы интерфейс знал, какие кнопки показывать
        return sorted(permission.value for permission in ROLE_PERMISSIONS[self.role])


class AdminCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    role: AdminRole = AdminRole.VIEWER
    # Пароль задал создатель учётки, значит знает его: админ сменит на свой при первом входе. Сервисной учётке — false.
    must_change_password: bool = True


class AdminUpdate(BaseModel):
    role: AdminRole | None = None
    is_active: bool | None = None
