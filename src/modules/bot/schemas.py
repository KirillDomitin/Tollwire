#!/usr/bin/python
# -*- coding: utf-8 -*-
import unicodedata
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from modules.bot.models.registration import RegistrationStatus
from modules.bot.telegram_html import TelegramHtmlError, check_telegram_html
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
    # Не заданное берётся из bot_settings (протокол, срок доступа, срок действия приглашения); консольный bot_invite
    # по-прежнему передаёт всё явно.
    protocol_id: UUID | None = None
    days: Annotated[int, Field(ge=1, le=3650)] | None = None
    expires_at: AwareDatetime | None = None
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
    used_by_user_id: UUID | None = None  # кто занял приглашение (в списке; пользователя могли удалить)
    used_by_name: str | None = None


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
    """Ответ боту: статус регистрации. Ссылку на конфиг бот просит отдельно (`config-link`)."""

    registration_id: UUID
    status: RegistrationStatus


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


class PublicConfigRead(BaseModel):
    """Содержимое страницы конфига: открывается по токену без входа. Приватный ключ клиента внутри `config`."""

    name: str
    valid_until: datetime
    server_name: str
    config: str  # .conf целиком
    share_link: str | None  # vpn://… для приложения AmneziaVPN
    share_qr: list[str] | None  # серия QR для сканера приложения
    file_name: str  # имя файла для скачивания без расширения: ASCII [A-Za-z0-9_-]


class ConfigLinkRead(BaseModel):
    """Ответ бота `config-link`: ссылка видна один раз, повторно её получить нельзя (в БД только хеш токена)."""

    config_url: str
    valid_until: datetime


class UserStatusRead(BaseModel):
    """/status в боте: имя, до какого числа доступ, протокол."""

    name: str
    valid_until: datetime | None
    protocol: str | None


# --- heartbeat -----------------------------------------------------------------------------------------------------


class HeartbeatRequest(BaseModel):
    # Имя бота подставляется в ссылку t.me/<имя>?start=<код>: только символы имени Telegram (допускается @ в начале).
    bot_username: Annotated[str, StringConstraints(pattern=r"^@?[A-Za-z0-9_]{1,32}$")]
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]


# --- настройки -----------------------------------------------------------------------------------------------------


class BotSettingsUpdate(BaseModel):
    """Настройки, которые админ меняет из интерфейса. Порог «панель недоступна» появится в UI вместе с 25.5."""

    default_protocol_id: UUID | None
    default_days: Annotated[int, Field(ge=1, le=3650)]
    invite_valid_days: Annotated[int, Field(ge=1, le=365)]


# --- рассылки ------------------------------------------------------------------------------------------------------

MAX_SELECTED_USERS = 1000


def _telegram_html(value: str) -> str:
    try:
        check_telegram_html(value)
    except TelegramHtmlError as error:
        raise PydanticCustomError(error.code, "The text is not valid Telegram HTML", {"position": error.position})
    return value


class AudienceRequest(BaseModel):
    """Кому адресована рассылка: вид и получатели. Так же задаётся отбор для подсчёта «сколько людей получит»."""

    kind: Literal["news", "config"]
    target: Literal["all", "selected", "protocol"]
    protocol_id: UUID | None = None
    user_ids: Annotated[list[UUID], Field(min_length=1, max_length=MAX_SELECTED_USERS)] | None = None

    @model_validator(mode="after")
    def _target_is_consistent(self) -> Self:
        if self.kind == "config" and self.target == "all":
            raise ValueError("Config links go to selected people or to a protocol, not to everybody")
        if self.target == "protocol" and self.protocol_id is None:
            raise ValueError("The protocol target needs protocol_id")
        if self.target == "selected" and not self.user_ids:
            raise ValueError("The selected target needs user_ids")
        return self


class BroadcastCreate(AudienceRequest):
    """Новая рассылка. kind: news (текст в Telegram HTML) или config (каждому новая ссылка на его конфиг)."""

    request_id: UUID  # повторный POST с тем же id возвращает ту же рассылку
    text: Annotated[str, AfterValidator(_telegram_html)] | None = None

    @model_validator(mode="after")
    def _news_has_text(self) -> Self:
        if self.kind == "news" and self.text is None:
            raise ValueError("A news broadcast needs a text")
        return self


# Коды, которые бот присылает вместо текста ошибки Telegram: в панель не попадает ничего из ответов Telegram.
OutboxErrorCode = Literal[
    "blocked", "chat_not_found", "bad_request", "network", "telegram_5xx", "flood", "too_many_attempts"
]


class OutboxAckItem(BaseModel):
    id: UUID
    status: Literal["sent", "blocked", "failed", "retry"]
    error: OutboxErrorCode | None = None
    retry_after: Annotated[int, Field(ge=0, le=86400)] | None = None


class AudienceRead(BaseModel):
    recipients: int  # получат
    without_telegram: int  # адресаты без привязанного Telegram
    blocked: int  # заблокировали бота: пропускаются


class BroadcastRead(BaseModel):
    id: UUID
    kind: str
    text: str | None
    target: str
    protocol_id: UUID | None
    created_at: datetime
    cancelled_at: datetime | None
    finished_at: datetime | None
    total: int
    sent: int
    blocked: int
    failed: int
    skipped: int
    cancelled: int
    state: Literal["running", "cancelled", "finished"]


class OutboxCounts(BaseModel):
    pending: int
    claimed: int


class BotStatusRead(BaseModel):
    """Состояние бота для страницы «Бот»: по последнему замеченному действующему ключу."""

    has_keys: bool  # есть хотя бы один ключ, в том числе отозванный: история остаётся доступной
    active_keys: int
    bot_username: str | None
    last_seen_at: datetime | None
    last_seen_version: str | None
    stale: bool  # бот давно не выходил на связь (или ещё ни разу)
    last_claim_at: datetime | None  # когда бот последний раз забирал очередь сообщений
    outbox: OutboxCounts


class BotSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    default_protocol_id: UUID | None
    default_days: int
    invite_valid_days: int
    panel_down_after_seconds: int  # использует 25.5; интерфейс его пока не показывает
    updated_at: datetime


class BotProtocolRead(BaseModel):
    id: UUID
    server_id: UUID
    server_name: str
    type: str


class RecipientRead(BaseModel):
    """Человек для выбора получателей рассылки. telegram_id наружу не отдаётся."""

    id: UUID
    username: str
    telegram_username: str | None
    bot_blocked_at: datetime | None


class OutboxRowRead(BaseModel):
    """Состояние одного сообщения очереди: адреса и telegram_id здесь нет."""

    id: UUID
    status: str
    error: str | None


class ConfigLinkQueued(BaseModel):
    outbox_id: UUID
    status: str


# --- очередь: Bot API ----------------------------------------------------------------------------------------------


class OutboxClaimRequest(BaseModel):
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class OutboxClaimItem(BaseModel):
    """news: id, kind, telegram_id, broadcast_id; config: id, kind, telegram_id, config_url, valid_until."""

    id: UUID
    kind: str
    telegram_id: int
    broadcast_id: UUID | None = None
    config_url: str | None = Field(default=None, repr=False)  # токен в адресе: в логи не попадает
    valid_until: datetime | None = None


class OutboxClaimResponse(BaseModel):
    lease_id: UUID
    lease_until: datetime
    items: list[OutboxClaimItem]
    texts: dict[UUID, str]  # текст новости один на рассылку


class OutboxAckRequest(BaseModel):
    lease_id: UUID
    results: Annotated[list[OutboxAckItem], Field(max_length=100)]


class OutboxAckResponse(BaseModel):
    applied: list[UUID]
    ignored: list[UUID]
