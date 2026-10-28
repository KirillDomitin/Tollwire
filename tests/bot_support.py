"""Общее для тестов бота (этап 25.1): сцены, обёртки над сервисами модуля `modules.bot`, константы контракта.

Тесты написаны до кода. Всё, что план не фиксирует (пути импорта, сигнатуры сервисов, имена полей ответов, причины
отказа), собрано ЗДЕСЬ: если реализация назвала что-то иначе, правится этот файл, а не десяток тестов.

Принятый контракт (из плана 25.1):

- модели: `modules.bot.models.{key,invite,registration}` (`BotKey`, `BotInvite`, `BotRegistration`,
  `RegistrationStatus`);
- сервисы: `BotKeyService(session, actor)`: `create(BotKeyCreate) -> (BotKey, открытый ключ)`, `revoke(id)`, `list()`;
  `BotInviteService(session, actor)`: `create(InviteCreate) -> (BotInvite, открытый код)`, `revoke(id)`, `list()`,
  `check(code) -> InviteStatus`; `BotRegistrationService(session, actor, payment=InviteFree())`:
  `register(RegistrationCreate, bot_key_id=None) -> BotRegistration`, `retry(id)`, `on_issue_finished(job)`,
  `reconcile()`;
- `payment.PaymentPolicy.check(registration) -> PaymentStatus` (`PAID` / `PENDING`), `InviteFree` всегда `PAID`;
- `invites.generate_code()`, `invites.invite_status(invite, now) -> InviteStatus`
  (значения `valid|used|expired|revoked`);
- `AccessGrantService.stage_create` — выделенная часть `create` без commit (в ней открывается окно гонки в
  тестах регистрации: к этому моменту пользователь и подписка уже во flush);
- `RegistrationConflictError.reason`: `invite_used`, `email_taken`, `telegram_taken`;
- Bot API отвечает JSON; ключ бота в заголовке `X-Bot-Key`;
  админский API — `/api/v1/bot-admin/{keys,invites,registrations}`.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from core.exceptions import (
    AlreadyExistsError,
    AppError,
    InactiveAccessError,
    NotFoundError,
    RegistrationConflictError,
    StateConflictError,
)
from main import app
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.bot.invites import BotInviteService, InviteStatus, generate_code, invite_status
from modules.bot.keys import BotKeyService
from modules.bot.models.invite import BotInvite
from modules.bot.models.key import BotKey
from modules.bot.models.registration import BotRegistration, RegistrationStatus
from modules.bot.payment import InviteFree, PaymentPolicy, PaymentStatus
from modules.bot.registrations import BotRegistrationService
from modules.bot.schemas import BotKeyCreate, InviteCreate, RegistrationCreate
from modules.jobs.models.job import Job, JobType
from modules.protocols.models.protocol import VPNProtocol
from modules.servers.models.server import VPNServer
from modules.vpn_users.models.user import VPNUser
from tests.fakes.fake_ssh import FakeServer
from tests.fixtures import World

if TYPE_CHECKING:  # модули этапа 25.3 подключаются внутри функций: файл не падает на импорте, пока кода нет
    from modules.bot.models.config_link import ConfigLink

__all__ = [
    "AppError",
    "InviteFree",
    "InviteStatus",
    "NotFoundError",
    "PaymentPolicy",
    "PaymentStatus",
    "RegistrationConflictError",
    "RegistrationCreate",
    "RegistrationStatus",
    "StateConflictError",
    "reg_status",
    "generate_code",
    "invite_status",
]

BOT_KEY_HEADER = "X-Bot-Key"

# Данные «первого» и «второго» человека в Telegram. telegram_id нарочно длинный: проверка «в аудите его нет» не должна
# случайно совпасть с цифрами из UUID.
TELEGRAM_ID = 5550001234
SECOND_TELEGRAM_ID = 5550004321
EMAIL = "ivan.petrov@example.org"
SECOND_EMAIL = "maria.sidorova@example.org"
NAME = "Иван Петров"
USERNAME = "ivan_p"

# Статусы приглашения (значения InviteStatus и поле `status` в ответах /invites/check и админского списка).
VALID, USED, EXPIRED, REVOKED, UNKNOWN = "valid", "used", "expired", "revoked", "unknown"

# Отказ по приглашению, которого нельзя использовать (нет такого / истекло / отозвано / протокол удалён): план не
# фиксирует,
# 404 это или 409 с причиной, — тесты проверяют только отказ и отсутствие побочных эффектов.
INVITE_REFUSED = (NotFoundError, RegistrationConflictError)
# Retry не по `failed`, повторный retry, retry по отозванному доступу: все исключения main.py сводит к 409.
RETRY_REFUSED = (StateConflictError, AlreadyExistsError, InactiveAccessError)
# Проигравший гонки за почту: свой отказ сервиса или IntegrityError от уникального индекса (main.py отдаёт его как 409).
RACE_LOSER_ERRORS = (RegistrationConflictError, IntegrityError)


def reg_status(value: str) -> RegistrationStatus:
    """Статус регистрации по значению (provisioning): для строк, которые тест кладёт в БД сам."""
    return RegistrationStatus(value)


def status_value(status: Any) -> str:
    """Значение статуса независимо от того, enum это или строка."""
    return str(getattr(status, "value", status))


def bot_action(name: str) -> AuditAction:
    """Событие аудита по значению ('bot_key_created'): нет такого - ValueError, а не AttributeError при импорте."""
    return AuditAction(name)


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- сервисы -------------------------------------------------------------------------------------------------------


async def create_key(
    session: AsyncSession, name: str = "test-bot", actor: AuditActor = SYSTEM_ACTOR
) -> tuple[BotKey, str]:
    """Ключ бота и его открытое значение (показывается один раз)."""
    return await BotKeyService(session, actor).create(BotKeyCreate(name=name))


async def create_invite(
    session: AsyncSession,
    protocol_id: UUID,
    *,
    days: int = 30,
    expires_in: timedelta = timedelta(days=7),
    note: str | None = None,
    actor: AuditActor = SYSTEM_ACTOR,
) -> tuple[BotInvite, str]:
    """Приглашение и его открытый код (показывается один раз)."""
    data = InviteCreate(protocol_id=protocol_id, days=days, expires_at=now() + expires_in, note=note)
    return await BotInviteService(session, actor).create(data)


async def check_invite(session: AsyncSession, code: str, actor: AuditActor = SYSTEM_ACTOR) -> str:
    """Статус приглашения по коду; «нет такого» — UNKNOWN (сервис отдаёт его статусом или NotFoundError)."""
    try:
        return status_value(await BotInviteService(session, actor).check(code))
    except NotFoundError:
        return UNKNOWN


def registration_data(
    code: str,
    *,
    telegram_id: int = TELEGRAM_ID,
    email: str = EMAIL,
    name: str = NAME,
    username: str | None = USERNAME,
) -> RegistrationCreate:
    return RegistrationCreate(code=code, telegram_id=telegram_id, telegram_username=username, name=name, email=email)


def registration_service(
    session: AsyncSession, payment: Any = None, actor: AuditActor = SYSTEM_ACTOR
) -> BotRegistrationService:
    if payment is None:
        return BotRegistrationService(session, actor)
    return BotRegistrationService(session, actor, payment=payment)


async def register(
    session: AsyncSession,
    code: str,
    *,
    key: BotKey | None = None,
    payment: Any = None,
    **fields: Any,
) -> BotRegistration:
    return await registration_service(session, payment).register(
        registration_data(code, **fields), bot_key_id=key.id if key else None
    )


class PendingPayment:
    """Заглушка платёжной политики: оплата не подтверждена. Утиная типизация: наследовать PaymentPolicy не нужно."""

    async def check(self, registration: BotRegistration) -> Any:
        return PaymentStatus.PENDING


# --- сцены ---------------------------------------------------------------------------------------------------------


@dataclass
class BotScene:
    server: VPNServer
    protocol: VPNProtocol
    fake: FakeServer
    key: BotKey
    key_secret: str


async def bot_scene(world: World) -> BotScene:
    """Сервер с установленным AmneziaWG на фейке и ключ бота."""
    server, protocol, fake = await world.server()
    assert protocol is not None
    key, secret = await create_key(world.session)
    return BotScene(server, protocol, fake, key, secret)


async def reload(session: AsyncSession, obj: Any) -> Any:
    """Свежие данные объекта из БД (его могли изменить воркер или другая сессия)."""
    await session.refresh(obj)
    return obj


async def fetch(model: type[Any], object_id: UUID) -> Any:
    """Строка из БД отдельной сессией: видно только то, что закоммичено."""
    async with session_factory() as session:
        return await session.get(model, object_id)


async def snapshot() -> dict[str, int]:
    """Сколько чего в БД (отдельная сессия: незакоммиченное не видно). Для проверки «побочных эффектов нет»."""
    async with session_factory() as session:

        async def count(query: Any) -> int:
            return int((await session.execute(query)).scalar_one())

        return {
            "users": await count(select(func.count()).select_from(VPNUser)),
            "subscriptions": await count(select(func.count()).select_from(Subscription)),
            "grants": await count(select(func.count()).select_from(AccessGrant)),
            "registrations": await count(select(func.count()).select_from(BotRegistration)),
            "issue_jobs": await count(select(func.count()).select_from(Job).where(Job.type == JobType.CREATE_USER)),
            "used_invites": await count(
                select(func.count()).select_from(BotInvite).where(BotInvite.used_at.is_not(None))
            ),
        }


async def bot_audit_dump() -> str:
    """Все записи аудита бота (`bot_*`) одной строкой: для проверки «секретов в аудите нет»."""
    async with session_factory() as session:
        logs = (await session.execute(select(AuditLog))).scalars().all()
    return "\n".join(
        f"{log.action.value} {log.entity_type} {log.entity_id} {log.meta}"
        for log in logs
        if log.action.value.startswith("bot_")
    )


async def audit_dump() -> str:
    """Весь журнал аудита одной строкой."""
    async with session_factory() as session:
        logs = (await session.execute(select(AuditLog))).scalars().all()
    return "\n".join(f"{log.action.value} {log.entity_type} {log.entity_id} {log.meta}" for log in logs)


# --- HTTP ----------------------------------------------------------------------------------------------------------


@asynccontextmanager
async def bot_client(key: str | None = None) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент бота: только X-Bot-Key (если задан), без JWT админа и без общих cookie."""
    headers = {BOT_KEY_HEADER: key} if key is not None else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
    ) as http:
        yield http


KEYS_URL = "/api/v1/bot-admin/keys"
INVITES_URL = "/api/v1/bot-admin/invites"
REGISTRATIONS_URL = "/api/v1/bot-admin/registrations"


def registration_body(code: str, **fields: Any) -> dict[str, Any]:
    data = registration_data(code, **fields)
    return {
        "code": data.code,
        "telegram_id": data.telegram_id,
        "telegram_username": data.telegram_username,
        "name": data.name,
        "email": data.email,
    }


async def admin_create_key(api: httpx.AsyncClient, name: str = "test-bot") -> dict[str, Any]:
    response = await api.post(KEYS_URL, json={"name": name})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    assert body["key"], "the key is shown once, at creation"
    return body


async def admin_create_invite(
    api: httpx.AsyncClient,
    protocol_id: UUID | str,
    *,
    days: int = 30,
    expires_in: timedelta = timedelta(days=7),
    note: str | None = None,
) -> dict[str, Any]:
    body = {
        "protocol_id": str(protocol_id),
        "days": days,
        "expires_at": (now() + expires_in).isoformat(),
        "note": note,
    }
    response = await api.post(INVITES_URL, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    assert created["code"], "the code is shown once, at creation"
    return created


def accepted(response: httpx.Response) -> bool:
    """POST /registrations: 201 (создана) или 200 (повтор того же человека) — код план не фиксирует."""
    return response.status_code in (200, 201)


def new_uuid() -> str:
    return str(uuid4())


# --- страница конфига (этап 25.3) ----------------------------------------------------------------------------------
#
# Тесты написаны до кода. Принятое из плана: `modules.bot.models.config_link.ConfigLink` (`config_links`: token_hash,
# credential_id, created_at, revoked_at, last_opened_at), `modules.bot.config_links.ConfigLinkService(session, actor)`,
# `POST /api/v1/bot/users/by-telegram/{id}/config-link` -> `{config_url, valid_until}`, `POST /api/v1/public/config
# {token}` -> `PublicConfigRead`, 409 `reason`: config_unavailable / not_ready / no_access / not_configured, один
# одинаковый 404 `{"detail": "Not found"}` на любой отказ при открытии.
#
# Допущения, которых в плане нет (правятся ТОЛЬКО здесь):
#
# - `ConfigLinkService.issue_for_telegram(telegram_id) -> (ConfigLink, token)` (как `BotKeyService.create`);
#   `ConfigLinkService.open(token) -> PublicConfigRead` (атрибуты name, valid_until, server_name, config, share_link,
#   share_qr, file_name); отказ открытия - `NotFoundError`; отказ выпуска - `ReasonConflictError` с `.reason`
#   (у `NotFoundError` для неизвестного telegram_id);
# - адрес панели берётся из `settings.public_url` / `panel_domain` / `https_port` в момент запроса (см.
#   `set_public_address`), а результат читается атрибутом `PUBLIC_ADDRESS_ATTR` у `Settings`;
# - успешный выпуск по HTTP - 200 или 201.

PUBLIC_BASE = "https://panel.example.test"
PUBLIC_CONFIG_URL = "/api/v1/public/config"
PUBLIC_ADDRESS_ATTR = "public_base_url"  # вычисляемый адрес панели у Settings: https://домен[:порт] или None
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{43}$")
NOT_FOUND_BODY = {"detail": "Not found"}
# Причины 409 выпуска ссылки (поле `reason`).
CONFIG_UNAVAILABLE, NOT_READY, NO_ACCESS, NOT_CONFIGURED = (
    "config_unavailable",
    "not_ready",
    "no_access",
    "not_configured",
)


def config_link_path(telegram_id: int = TELEGRAM_ID) -> str:
    return f"/api/v1/bot/users/by-telegram/{telegram_id}/config-link"


def set_public_address(monkeypatch: Any, url: str = PUBLIC_BASE, *, domain: str = "", port: int = 443) -> None:
    """Публичный адрес панели в настройках (по умолчанию тесты работают с PUBLIC_BASE, см. tests/conftest.py)."""
    from core.config import settings

    monkeypatch.setattr(settings, "public_url", url)
    monkeypatch.setattr(settings, "panel_domain", domain)
    monkeypatch.setattr(settings, "https_port", port)


def token_of(config_url: str) -> str:
    """Токен из адреса `<панель>/c#<токен>`."""
    base, _, token = config_url.partition("#")
    assert base == f"{PUBLIC_BASE}/c", config_url
    return token


@dataclass
class IssuedLink:
    link: "ConfigLink"
    token: str


async def issue_link(
    session: AsyncSession, telegram_id: int = TELEGRAM_ID, actor: AuditActor = SYSTEM_ACTOR
) -> IssuedLink:
    from modules.bot.config_links import ConfigLinkService

    link, token = await ConfigLinkService(session, actor).issue_for_telegram(telegram_id)
    return IssuedLink(link, token)


async def open_link(session: AsyncSession, token: str, actor: AuditActor = SYSTEM_ACTOR) -> Any:
    from modules.bot.config_links import ConfigLinkService

    return await ConfigLinkService(session, actor).open(token)


async def links_in_db() -> list["ConfigLink"]:
    """Все строки config_links отдельной сессией (только закоммиченное)."""
    from modules.bot.models.config_link import ConfigLink

    async with session_factory() as session:
        return list((await session.execute(select(ConfigLink).order_by(ConfigLink.created_at))).scalars())


async def live_links_in_db() -> list["ConfigLink"]:
    return [link for link in await links_in_db() if link.revoked_at is None]


async def activate_registration(
    world: World, scene: BotScene, worker: Any, *, telegram_id: int = TELEGRAM_ID, email: str = EMAIL
) -> BotRegistration:
    """Регистрация бота доведена воркером до `active`: у человека есть живой клиент на фейковом сервере."""
    from tests.integration.conftest import drain

    _, code = await create_invite(world.session, scene.protocol.id)
    registration = await register(world.session, code, key=scene.key, telegram_id=telegram_id, email=email)
    await drain(worker)
    await reload(world.session, registration)
    assert status_value(registration.status) == "active"
    return registration


async def http_activate(
    api: httpx.AsyncClient, bot: httpx.AsyncClient, protocol_id: UUID | str, worker: Any, **fields: Any
) -> dict[str, Any]:
    """Через HTTP: приглашение админа -> POST /registrations бота -> воркер -> active. Возвращает тело регистрации."""
    from tests.integration.conftest import drain

    invite = await admin_create_invite(api, protocol_id)
    response = await bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"], **fields))
    assert accepted(response), response.text
    await drain(worker)
    body: dict[str, Any] = response.json()
    return body


async def request_link(bot: httpx.AsyncClient, telegram_id: int = TELEGRAM_ID) -> httpx.Response:
    return await bot.post(config_link_path(telegram_id))


async def open_over_http(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post(PUBLIC_CONFIG_URL, json={"token": token})


# --- очередь сообщений, рассылки, настройки бота (этап 25.4) -------------------------------------------------------
#
# Тесты написаны до кода (план: docs/plans/telegram-bot-25-4.md). Принятое из плана: таблицы `bot_settings` (одна строка
# `id = 1`), `bot_broadcasts`, `bot_outbox`; колонки `vpn_users.bot_blocked_at`, `bot_keys.last_claim_at`; Bot API
# `POST /api/v1/bot/outbox/{claim,ack}`; админский API `/api/v1/bot-admin/`: `status`, `settings`, `protocols`,
# `recipients`, `invites`, `registrations`, `broadcasts`, `broadcasts/audience`, `broadcasts/{id}`,
# `broadcasts/{id}/cancel`, `users/{id}/config-link`, `outbox/{id}`;
# события аудита `bot_broadcast_created`, `bot_broadcast_cancelled`, `bot_config_link_queued`, `bot_settings_updated`;
# лимиты в `core.config.settings`.
#
# Допущения, которых в плане нет (правятся ТОЛЬКО здесь):
#
# - модели: `modules.bot.models.settings.BotSettings`, `modules.bot.models.broadcast.BotBroadcast`,
#   `modules.bot.models.outbox.BotOutbox` - колонки по плану под теми же именами; `kind`, `target`, `status` - строки
#   (или enum со значениями плана; сравниваем через `status_value`);
# - сервисы: `modules.bot.settings.BotSettingsService(session, actor)`: `get()` (создаёт строку `id = 1`, если её нет:
#   тестовая БД чистится TRUNCATE, миграция строку вставляет один раз), `update(BotSettingsUpdate)`;
#   `modules.bot.outbox.OutboxService(session, actor)`: `create_broadcast(BroadcastCreate) -> BotBroadcast`,
#   `audience(BroadcastCreate) -> {recipients, without_telegram, blocked}`, `cancel_broadcast(id) -> BotBroadcast`,
#   `queue_config_link(user_id) -> BotOutbox`, `claim(key_id, limit)`, `ack(lease_id, items)`;
#   результат `claim` - атрибуты `lease_id`, `lease_until`, `items` (у элемента `id`, `kind`, `telegram_id`,
#   `broadcast_id`, `config_url`, `valid_until`), `texts` (`{broadcast_id: text}`); результат `ack` - `applied` и
#   `ignored` (списки id); `ConfigLinkService.stage_issue(user_id, grant_id, *, source, meta) -> (ConfigLink, token)`
#   без commit; текст из одних пробелов и переводов строк для `telegram_html` - пустой (`telegram_html_empty`);
# - схемы в `modules.bot.schemas`: `BroadcastCreate(request_id, kind, text, target, protocol_id, user_ids)`,
#   `OutboxAckItem(id, status, error, retry_after)`, `BotSettingsUpdate(default_protocol_id, default_days,
#   invite_valid_days)`; `InviteCreate.protocol_id/days/expires_at` необязательны;
# - `modules.bot.telegram_html.check_telegram_html(text) -> int` (видимая длина в единицах UTF-16) бросает
#   `TelegramHtmlError` с `.code` (`telegram_html_tag|attr|entity|nesting|length|empty`) и `.position` (смещение
#   в исходном тексте, с 0); запрещённые символы и голые `<`/`&` - код `telegram_html_entity`;
# - причины 409 в дополнение к 25.3: `no_telegram`, `bot_blocked` (индивидуальная отправка), `no_default_protocol`
#   (приглашение без протокола); коды `error` строки очереди - перечень плана плюс `no_telegram`, `no_access`,
#   `config_unavailable`, `not_configured`, `not_ready`, `too_many_attempts`;
# - 422 неверного HTML: `detail[0].type` - код, `detail[0].ctx.position` - число, поля `input` нет;
# - настройки читаются при каждом обращении (`settings.bot_outbox_lease_seconds`, `bot_outbox_max_attempts`,
#   `bot_broadcast_max_recipients`, `bot_outbox_retention_days`): тесты подменяют их monkeypatch'ем;
# - списки админского API (`/invites`, `/registrations`, `/broadcasts`) - массив, новые первыми; курсор - как у
#   `/audit-logs` (`before_created_at` + `before_id` из последней строки, `limit` 1..100); у рассылки поле `state`:
#   `running` / `cancelled` / `finished`; у человека в `/recipients` - `id`, `username`, `telegram_username`,
#   `bot_blocked_at`;
# - лимит админа считается первым делом в обработчике (до проверок), по одному вызову на запрос.

NEWS, CONFIG = "news", "config"
TARGET_ALL, TARGET_SELECTED, TARGET_PROTOCOL = "all", "selected", "protocol"
PENDING, CLAIMED, SENT, BLOCKED, FAILED, SKIPPED, CANCELLED = (
    "pending",
    "claimed",
    "sent",
    "blocked",
    "failed",
    "skipped",
    "cancelled",
)
OPEN_STATUSES = (PENDING, CLAIMED)
PRIORITY_INDIVIDUAL, PRIORITY_BROADCAST = 0, 10
NO_TELEGRAM, BOT_BLOCKED, NO_DEFAULT_PROTOCOL = "no_telegram", "bot_blocked", "no_default_protocol"
TOO_MANY_ATTEMPTS = "too_many_attempts"
NOT_READY_GIVE_UP_MINUTES = 30  # дольше этого строка `config` с `not_ready` пропускается

NEWS_MARKER = "Секретная-новость-8842"  # по нему видно, что текст новости не попал в аудит и логи
NEWS_TEXT = f"<b>Плановые работы</b> сегодня в 23:00. {NEWS_MARKER}"

ADMIN_URL = "/api/v1/bot-admin"
STATUS_URL = f"{ADMIN_URL}/status"
SETTINGS_URL = f"{ADMIN_URL}/settings"
PROTOCOLS_URL = f"{ADMIN_URL}/protocols"
RECIPIENTS_URL = f"{ADMIN_URL}/recipients"
BROADCASTS_URL = f"{ADMIN_URL}/broadcasts"
AUDIENCE_URL = f"{ADMIN_URL}/broadcasts/audience"
OUTBOX_CLAIM_URL = "/api/v1/bot/outbox/claim"
OUTBOX_ACK_URL = "/api/v1/bot/outbox/ack"


def lease_seconds() -> int:
    from core.config import settings

    return int(getattr(settings, "bot_outbox_lease_seconds"))


def max_attempts() -> int:
    from core.config import settings

    return int(getattr(settings, "bot_outbox_max_attempts"))


def user_config_link_url(user_id: UUID | str) -> str:
    return f"{ADMIN_URL}/users/{user_id}/config-link"


def outbox_url(outbox_id: UUID | str) -> str:
    return f"{ADMIN_URL}/outbox/{outbox_id}"


def broadcast_url(broadcast_id: UUID | str, tail: str = "") -> str:
    return f"{BROADCASTS_URL}/{broadcast_id}{tail}"


# --- данные --------------------------------------------------------------------------------------------------------


async def add_person(
    session: AsyncSession,
    telegram_id: int | None = TELEGRAM_ID,
    name: str = "Person",
    *,
    blocked: bool = False,
    telegram_username: str | None = None,
) -> VPNUser:
    """Человек без доступа (для новостей доступ не нужен): только имя, почта и Telegram."""
    extra: dict[str, Any] = {"bot_blocked_at": now()} if blocked else {}
    user = VPNUser(
        username=name,
        email=f"{uuid4().hex[:10]}@example.org",
        telegram_id=telegram_id,
        telegram_username=telegram_username,
        **extra,
    )
    session.add(user)
    await session.commit()
    return user


async def add_people(session: AsyncSession, count: int, *, first_telegram_id: int = 7_000_000_000) -> list[VPNUser]:
    """Много людей с Telegram одним коммитом: telegram_id идут подряд от first_telegram_id."""
    users = [
        VPNUser(username=f"Person {index}", email=f"person{index}@example.org", telegram_id=first_telegram_id + index)
        for index in range(count)
    ]
    session.add_all(users)
    await session.commit()
    return users


async def issued_person(
    world: World, telegram_id: int | None = TELEGRAM_ID, protocol: VPNProtocol | None = None
) -> tuple[VPNUser, Subscription, AccessGrant, Any]:
    """Человек с живым клиентом, выданным из панели, и привязанным Telegram (как `issued_user` в тестах 25.3)."""
    if protocol is None:
        _, protocol, _ = await world.server()
    assert protocol is not None
    user, subscription, grant, credential = await world.issued(protocol)
    user.telegram_id = telegram_id
    await world.session.commit()
    return user, subscription, grant, credential


async def sql(statement: str, **params: Any) -> None:
    """Прямая запись в БД отдельной сессией (подготовить состояние: состарить аренду, сменить `not_before`)."""
    async with session_factory() as session:
        await session.execute(text(statement), params)
        await session.commit()


async def sql_rows(statement: str, **params: Any) -> list[tuple[Any, ...]]:
    async with session_factory() as session:
        return [tuple(row) for row in (await session.execute(text(statement), params)).all()]


async def tables_dump(*tables: str) -> str:
    """Все строки таблиц одной строкой: для проверки «токена и адреса ссылки нет ни в одной колонке»."""
    async with session_factory() as session:
        return "\n".join(
            str((await session.execute(text(f"SELECT * FROM {table}"))).all())  # noqa: S608 - имена из тестов
            for table in tables
        )


def broadcast_data(
    *,
    kind: str = NEWS,
    target: str = TARGET_ALL,
    text: str | None = None,
    protocol_id: UUID | None = None,
    user_ids: list[UUID] | None = None,
    request_id: UUID | None = None,
) -> Any:
    from modules.bot.schemas import BroadcastCreate

    if kind == NEWS and text is None:
        text = NEWS_TEXT
    return BroadcastCreate(
        request_id=request_id or uuid4(),
        kind=kind,
        text=text,
        target=target,
        protocol_id=protocol_id,
        user_ids=user_ids,
    )


def broadcast_body(
    *,
    kind: str = NEWS,
    target: str = TARGET_ALL,
    text: str | None = None,
    protocol_id: UUID | str | None = None,
    user_ids: list[UUID | str] | None = None,
    request_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Тело `POST /broadcasts`: пустые поля не отправляются."""
    if kind == NEWS and text is None:
        text = NEWS_TEXT
    body: dict[str, Any] = {"request_id": str(request_id or uuid4()), "kind": kind, "target": target}
    if text is not None:
        body["text"] = text
    if protocol_id is not None:
        body["protocol_id"] = str(protocol_id)
    if user_ids is not None:
        body["user_ids"] = [str(user_id) for user_id in user_ids]
    return body


def audience_body(**fields: Any) -> dict[str, Any]:
    """Тело `POST /broadcasts/audience`: тот же отбор, без `request_id` и без текста."""
    body = broadcast_body(**fields)
    body.pop("request_id", None)
    body.pop("text", None)
    return body


def outbox_service(session: AsyncSession, actor: AuditActor = SYSTEM_ACTOR) -> Any:
    from modules.bot.outbox import OutboxService

    return OutboxService(session, actor)


async def create_broadcast(session: AsyncSession, actor: AuditActor = SYSTEM_ACTOR, **fields: Any) -> Any:
    return await outbox_service(session, actor).create_broadcast(broadcast_data(**fields))


async def audience_of(session: AsyncSession, **fields: Any) -> Any:
    return await outbox_service(session).audience(broadcast_data(**fields))


async def cancel_broadcast(session: AsyncSession, broadcast_id: UUID, actor: AuditActor = SYSTEM_ACTOR) -> Any:
    return await outbox_service(session, actor).cancel_broadcast(broadcast_id)


async def queue_link(session: AsyncSession, user_id: UUID, actor: AuditActor = SYSTEM_ACTOR) -> Any:
    return await outbox_service(session, actor).queue_config_link(user_id)


async def claim(session: AsyncSession, key: BotKey, limit: int = 50) -> Any:
    return await outbox_service(session).claim(key.id, limit)


def ack_item(item_id: UUID, status: str, *, error: str | None = None, retry_after: int | None = None) -> Any:
    from modules.bot.schemas import OutboxAckItem

    return OutboxAckItem(id=item_id, status=status, error=error, retry_after=retry_after)


async def ack(session: AsyncSession, lease_id: UUID, *items: Any) -> Any:
    return await outbox_service(session).ack(lease_id, list(items))


async def ack_all(session: AsyncSession, claimed: Any, status: str = SENT) -> Any:
    """Подтвердить все выданные строки одним статусом."""
    return await ack(session, claimed.lease_id, *[ack_item(item.id, status) for item in claimed.items])


async def outbox_rows(**where: Any) -> list[Any]:
    """Строки очереди отдельной сессией, по порядку постановки; `where` - равенства по колонкам модели (строки и enum
    сравниваются по значению)."""
    from modules.bot.models.outbox import BotOutbox

    async with session_factory() as session:
        rows = list((await session.execute(select(BotOutbox).order_by(BotOutbox.created_at, BotOutbox.id))).scalars())

    def same(row: Any, column: str, expected: Any) -> bool:
        actual = getattr(row, column)
        return status_value(actual) == expected if isinstance(expected, str) else bool(actual == expected)

    return [row for row in rows if all(same(row, column, value) for column, value in where.items())]


async def outbox_row(outbox_id: UUID) -> Any:
    from modules.bot.models.outbox import BotOutbox

    return await fetch(BotOutbox, outbox_id)


async def broadcast_row(broadcast_id: UUID) -> Any:
    from modules.bot.models.broadcast import BotBroadcast

    return await fetch(BotBroadcast, broadcast_id)


async def user_row(user_id: UUID) -> Any:
    return await fetch(VPNUser, user_id)


def statuses(rows: list[Any]) -> list[str]:
    return [status_value(row.status) for row in rows]


COUNTER_NAMES = ("total", "sent", "blocked", "failed", "skipped", "cancelled")


def counters(broadcast: Any) -> dict[str, int]:
    return {name: int(getattr(broadcast, name)) for name in COUNTER_NAMES}


async def assert_counters_consistent(broadcast_id: UUID, *, dropped: int = 0) -> None:
    """Инвариант Р4-A: total = сумма итогов + открытые строки + выбывшие (строки удалённых людей)."""
    broadcast = await broadcast_row(broadcast_id)
    rows = await outbox_rows(broadcast_id=broadcast_id)
    shown = counters(broadcast)
    done = sum(shown[name] for name in COUNTER_NAMES if name != "total")
    opened = statuses(rows).count(PENDING) + statuses(rows).count(CLAIMED)
    assert shown["total"] == done + opened + dropped, (shown, statuses(rows))
    if dropped == 0:  # строки не чистились и не удалялись: каждый счётчик равен числу строк со своим статусом
        for name in (SENT, BLOCKED, FAILED, SKIPPED, CANCELLED):
            assert shown[name] == statuses(rows).count(name), (name, shown, statuses(rows))


async def expire_lease(outbox_id: UUID | None = None) -> None:
    """Аренда вышла (у одной строки или у всех выданных): `claimed_until` в прошлом, строка всё ещё `claimed`."""
    if outbox_id is None:
        await sql("UPDATE bot_outbox SET claimed_until = now() - interval '1 second' WHERE status = 'claimed'")
    else:
        await sql(
            "UPDATE bot_outbox SET claimed_until = now() - interval '1 second' WHERE id = :id AND status = 'claimed'",
            id=outbox_id,
        )


async def make_ready(outbox_id: UUID | None = None) -> None:
    """`not_before` в прошлом: отложенная строка снова годится к выдаче."""
    if outbox_id is None:
        await sql("UPDATE bot_outbox SET not_before = now() - interval '1 second'")
    else:
        await sql("UPDATE bot_outbox SET not_before = now() - interval '1 second' WHERE id = :id", id=outbox_id)


async def http_claim(bot: httpx.AsyncClient, limit: int = 50) -> httpx.Response:
    return await bot.post(OUTBOX_CLAIM_URL, json={"limit": limit})


async def http_ack(bot: httpx.AsyncClient, lease_id: str | UUID, results: list[dict[str, Any]]) -> httpx.Response:
    return await bot.post(OUTBOX_ACK_URL, json={"lease_id": str(lease_id), "results": results})


def ack_result(item_id: str | UUID, status: str = SENT, **fields: Any) -> dict[str, Any]:
    return {"id": str(item_id), "status": status, **fields}


async def admin_broadcast(api: httpx.AsyncClient, **fields: Any) -> httpx.Response:
    return await api.post(BROADCASTS_URL, json=broadcast_body(**fields))


async def admin_create_broadcast(api: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    response = await admin_broadcast(api, **fields)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created
