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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select
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
