#!/usr/bin/python
# -*- coding: utf-8 -*-
import unicodedata
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from modules.bot.models.registration import RegistrationStatus
from modules.vpn_users.schemas import Email


def _no_control_characters(value: str) -> str:
    # Ввод из Telegram недоверенный: переводы строк, NUL, ESC в имени ломают логи и интерфейс, а невидимые символы
    # (Cf: RLO, нулевой ширины; Zl/Zp: разделители строк) позволяют подменить то, что админ видит на экране.
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in value):
        raise ValueError("Control and invisible characters are not allowed")
    return value


Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64), AfterValidator(_no_control_characters)
]
TelegramUsername = Annotated[str, StringConstraints(max_length=64), AfterValidator(_no_control_characters)]
TelegramId = Annotated[int, Field(ge=1, le=2**63 - 1)]  # BIGINT
# Код приглашения - параметр start ссылки t.me/<бот>?start=<код>: до 64 символов.
Code = Annotated[str, Field(min_length=1, max_length=64)]


# --- ключи ---------------------------------------------------------------------------------------------------------


class BotKeyCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class BotKeyRead(BaseModel):
    """Ключ без секрета: открытое значение показывается один раз, при создании."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    key_prefix: str
    created_at: datetime
    last_seen_at: datetime | None
    last_seen_version: str | None
    bot_username: str | None
    revoked_at: datetime | None


class BotKeyCreated(BotKeyRead):
    key: str


# --- приглашения ---------------------------------------------------------------------------------------------------


class InviteCreate(BaseModel):
    # bot_settings (значения по умолчанию) появятся позже: пока протокол и срок обязательны.
    protocol_id: UUID
    days: Annotated[int, Field(ge=1, le=3650)]
    expires_at: AwareDatetime
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None


class InviteRead(BaseModel):
    id: UUID
    protocol_id: UUID | None
    days: int
    expires_at: datetime
    note: str | None
    created_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    status: str  # valid / used / expired / revoked


class InviteCreated(InviteRead):
    code: str  # показывается один раз
    link: str | None  # t.me/<бот>?start=<код>, если известно имя бота (из heartbeat)


class InviteCheckRequest(BaseModel):
    code: Code


class InviteCheckResponse(BaseModel):
    status: str  # valid / used / expired / revoked / unknown; без подробностей приглашения


# --- регистрации ---------------------------------------------------------------------------------------------------


class RegistrationCreate(BaseModel):
    code: Code
    telegram_id: TelegramId
    telegram_username: TelegramUsername | None = None
    name: Name
    email: Email


class BotRegistrationRead(BaseModel):
    """Ответ боту: статус регистрации и, когда будет страница конфига (25.3), ссылка на неё."""

    registration_id: UUID
    status: RegistrationStatus
    config_url: str | None = None


class RegistrationRead(BaseModel):
    """Регистрация для админа."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    invite_id: UUID
    status: RegistrationStatus
    name: str
    email: str
    telegram_id: int
    telegram_username: str | None
    user_id: UUID | None
    access_grant_id: UUID | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class UserStatusRead(BaseModel):
    """/status в боте: имя, до какого числа доступ, протокол."""

    name: str
    valid_until: datetime | None
    protocol: str | None
    config_url: str | None = None


# --- heartbeat -----------------------------------------------------------------------------------------------------


class HeartbeatRequest(BaseModel):
    # Имя бота подставляется в ссылку t.me/<имя>?start=<код>: только символы имени Telegram (допускается @ в начале).
    bot_username: Annotated[str, StringConstraints(pattern=r"^@?[A-Za-z0-9_]{1,32}$")]
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
