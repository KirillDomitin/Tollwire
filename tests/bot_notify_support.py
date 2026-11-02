"""Общее для тестов этапа 25.5 (уведомления админу): привязка Telegram к админу, сборщик событий, очередь `notify`.

Тесты написаны до кода (план: docs/plans/telegram-bot-25-5.md, решения Р1-Р11 = A). Всё, что план не фиксирует (пути
импорта, сигнатуры сервисов, имена полей), собрано ЗДЕСЬ: если реализация назвала что-то иначе, правится этот файл, а не
десятки тестов. Модули 25.5 подключаются внутри функций: файл тестов не падает на импорте, пока кода нет.

Принятое из плана: таблицы `admin_telegram_links`, `bot_notify_cursor` (одна строка `id = 1`), `bot_notify_seen`;
колонки `bot_outbox.{admin_link_id,text,format,expires_at,notify_key}` и `bot_keys.last_seen_local`; Bot API
`POST /outbox/claim {limit, kinds?}`, `POST /heartbeat {..., local?}` -> `{notify: {panel_down_after_seconds, chats}}`,
`POST /admin-links {code, telegram_id, telegram_username?}`; админский API `/api/v1/bot-admin/{me/telegram*,
broadcasts/test, notify-events, admin-links}`; действия аудита `server_health_changed`, `bot_admin_link_*`,
`bot_notify_test_queued`.

Допущения, которых в плане нет (правятся ТОЛЬКО здесь):

- `modules.bot.notify.NotifyCollector(session).run()` - один проход сборщика (сам коммитит); возвращает число
  поставленных в очередь строк (или None - тесты результат не читают);
- `modules.bot.notify_catalog.event_keys(action: str, meta: dict | None) -> list[str]` - ключи каталога, которые
  порождает запись аудита (пусто - не уведомляет); `default_events() -> dict[str, bool]` - события админа по умолчанию;
  `CATALOG` - словарь, ключи которого - ключи каталога;
- `admin_telegram_links.events` - JSON-объект `{ключ каталога: bool}`; `chats` в ответе heartbeat - список
  `telegram_id` (чат с админом совпадает с его `telegram_id`);
- `modules.bot.admin_links.AdminLinkService(session, actor)`: `issue_code(admin_id) -> (code, expires_at)`,
  `link(code, telegram_id, telegram_username=None) -> объект с admin_name`, `unlink(admin_id)`,
  `set_events(admin_id, events)`, `chats() -> list[int]`; отказ привязки - `ReasonConflictError` с `.reason`;
- `OutboxService.claim(key_id, limit, kinds=None)`: у элемента `notify` атрибуты `text` и `format`;
- текст уведомления - обычный текст в `bot_outbox.text`, `format = 'plain'`, `notify_key` - ключ каталога.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx

from core.database.postgres import session_factory
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.audit.models.audit_log import AuditAction
from tests.bot_support import (
    ADMIN_URL,
    OUTBOX_CLAIM_URL,
    http_ack,
    outbox_rows,
    outbox_service,
    sql,
    sql_rows,
)

TG_ADMIN = 6660001234  # telegram_id админа: длинный, чтобы проверка «в тексте и аудите его нет» не совпала случайно
TG_ADMIN_2 = 6660004321
LINK_CODE_LENGTH = 32

ME_URL = f"{ADMIN_URL}/me/telegram"
ME_CODE_URL = f"{ME_URL}/code"
ME_EVENTS_URL = f"{ME_URL}/events"
ME_TEST_URL = f"{ME_URL}/test"
BROADCAST_TEST_URL = f"{ADMIN_URL}/broadcasts/test"
NOTIFY_EVENTS_URL = f"{ADMIN_URL}/notify-events"
ADMIN_LINKS_URL = f"{ADMIN_URL}/admin-links"  # админский список «кто получает»
BOT_HEARTBEAT_URL = "/api/v1/bot/heartbeat"
BOT_ADMIN_LINKS_URL = "/api/v1/bot/admin-links"  # Bot API: привязка по коду

# Ключи каталога (план, раздел «Каталог событий»).
SERVER_DOWN, SERVER_RECOVERED = "server_down", "server_recovered"
GATEWAY_ALL_DOWN, GATEWAY_FAILOVER, GATEWAY_RECOVERED = (
    "gateway_all_exits_down",
    "gateway_failover",
    "gateway_exit_recovered",
)
JOBS_FAILED, REGISTRATION_FAILED, HOST_KEY_MISMATCH = (
    "jobs_failed",
    "bot_registration_failed",
    "server_host_key_mismatch",
)
LOGIN_LOCKED, PANEL_DOWN = "login_locked", "panel_down"
ON_BY_DEFAULT = [
    SERVER_DOWN,
    SERVER_RECOVERED,
    GATEWAY_ALL_DOWN,
    GATEWAY_FAILOVER,
    GATEWAY_RECOVERED,
    JOBS_FAILED,
    REGISTRATION_FAILED,
    HOST_KEY_MISMATCH,
    PANEL_DOWN,
]
ALL_KEYS = [*ON_BY_DEFAULT, LOGIN_LOCKED]

# Причины 409 привязки (поле `reason`).
LINK_INVALID, LINK_EXPIRED, TELEGRAM_TAKEN = "link_invalid", "link_expired", "telegram_taken"
NOT_LINKED = "not_linked"

HEALTH_CHANGED = "server_health_changed"
BIDI = "‮⁦‏\u0007"  # управляющие и bidi-символы, которые из имён вырезаются


def minutes_ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def default_events() -> dict[str, bool]:
    return {key: key in ON_BY_DEFAULT for key in ALL_KEYS}


# --- привязки -------------------------------------------------------------------------------------------------------


async def make_link(
    admin_id: UUID,
    telegram_id: int | None = TG_ADMIN,
    *,
    events: dict[str, bool] | None = None,
    blocked: bool = False,
    username: str | None = "admin_tg",
) -> UUID:
    """Привязка админа напрямую в БД (минуя код): telegram_id задан - привязка «живая». Возвращает id привязки."""
    link_id = uuid4()
    linked = telegram_id is not None
    await sql(
        "INSERT INTO admin_telegram_links (id, admin_id, telegram_id, telegram_username, linked_at, blocked_at, "
        "events, created_at, updated_at) VALUES (:id, :admin, :tg, :un, "
        "CASE WHEN :linked THEN now() ELSE NULL END, CASE WHEN :blocked THEN now() ELSE NULL END, "
        "CAST(:events AS jsonb), now(), now())",
        id=link_id,
        admin=admin_id,
        tg=telegram_id,
        un=username if linked else None,
        linked=linked,
        blocked=blocked,
        events=json.dumps(events if events is not None else default_events()),
    )
    return link_id


async def link_rows() -> list[dict[str, Any]]:
    columns = ("id", "admin_id", "telegram_id", "telegram_username", "linked_at", "blocked_at", "events", "code_hash")
    rows = await sql_rows(f"SELECT {', '.join(columns)} FROM admin_telegram_links ORDER BY created_at")  # noqa: S608
    return [dict(zip(columns, row)) for row in rows]


async def link_of(admin_id: UUID) -> dict[str, Any] | None:
    return next((row for row in await link_rows() if row["admin_id"] == admin_id), None)


async def admin_login(role: Any = None) -> Any:
    """Администратор (по умолчанию superadmin) отдельной сессией: `AdminLogin` из integration/conftest."""
    from modules.auth.models.admin import AdminRole
    from tests.integration.conftest import make_admin

    async with session_factory() as session:
        return await make_admin(session, role or AdminRole.SUPERADMIN)


async def deactivate_admin(admin_id: UUID) -> None:
    await sql("UPDATE admins SET is_active = false WHERE id = :id", id=admin_id)


# --- журнал аудита и курсор -----------------------------------------------------------------------------------------


async def add_audit(
    action: str,
    *,
    entity_type: str | None = "server",
    entity_id: UUID | None = None,
    meta: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> UUID:
    """Запись аудита напрямую в БД с заданным `created_at` (так воспроизводится «now() начала транзакции»)."""
    audit_id = uuid4()
    await sql(
        "INSERT INTO audit_logs (id, action, entity_type, entity_id, metadata, created_at) "
        "VALUES (:id, :action, :etype, :eid, CAST(:meta AS jsonb), :at)",
        id=audit_id,
        action=AuditAction(action).name,
        etype=entity_type,
        eid=entity_id,
        meta=json.dumps(meta) if meta is not None else None,
        at=at or datetime.now(timezone.utc),
    )
    return audit_id


async def add_health_event(
    server_id: UUID,
    old: str | None,
    new: str,
    *,
    reason: str | None = None,
    at: datetime | None = None,
) -> UUID:
    meta: dict[str, Any] = {"from": old, "to": new}
    if reason is not None:
        meta["reason"] = reason
    return await add_audit(HEALTH_CHANGED, entity_id=server_id, meta=meta, at=at)


async def set_cursor(at: datetime) -> None:
    await sql(
        "INSERT INTO bot_notify_cursor (id, cursor_at, started_at) VALUES (1, :at, :at) "
        "ON CONFLICT (id) DO UPDATE SET cursor_at = EXCLUDED.cursor_at, started_at = EXCLUDED.started_at",
        at=at,
    )


async def cursor() -> datetime | None:
    rows = await sql_rows("SELECT cursor_at FROM bot_notify_cursor WHERE id = 1")
    return rows[0][0] if rows else None


async def seen_ids() -> set[UUID]:
    return {row[0] for row in await sql_rows("SELECT audit_log_id FROM bot_notify_seen")}


async def collect() -> Any:
    """Один проход сборщика `bot_notify` в своей сессии."""
    from modules.bot.notify import NotifyCollector

    async with session_factory() as session:
        return await NotifyCollector(session).run()


async def notify_rows(**where: Any) -> list[Any]:
    return await outbox_rows(kind="notify", **where)


async def audit_rows(action: str) -> list[tuple[Any, ...]]:
    rows = await sql_rows(
        "SELECT admin_id, entity_type, entity_id, metadata FROM audit_logs WHERE action = :a ORDER BY created_at",
        a=AuditAction(action).name,
    )
    return [(a, e, i, json.loads(m) if isinstance(m, str) else m) for a, e, i, m in rows]


# --- сервис привязки ------------------------------------------------------------------------------------------------


def link_service(session: Any, actor: AuditActor = SYSTEM_ACTOR) -> Any:
    from modules.bot.admin_links import AdminLinkService

    return AdminLinkService(session, actor)


async def issue_code(session: Any, admin_id: UUID, actor: AuditActor = SYSTEM_ACTOR) -> tuple[str, datetime]:
    code, expires_at = await link_service(session, actor).issue_code(admin_id)
    return code, expires_at


async def bind(session: Any, code: str, telegram_id: int = TG_ADMIN, username: str | None = "admin_tg") -> Any:
    return await link_service(session).link(code, telegram_id, username)


async def queue_notify(
    link_id: UUID,
    text: str = "Сервер srv недоступен",
    *,
    key: str = "server_down",
    expires_in: timedelta = timedelta(hours=24),
    fmt: str = "plain",
) -> UUID:
    """Строка `notify` в очереди напрямую (как её ставит сборщик)."""
    outbox_id = uuid4()
    await sql(
        "INSERT INTO bot_outbox (id, kind, admin_link_id, status, priority, text, format, expires_at, notify_key, "
        "created_at, updated_at) VALUES (:id, 'notify', :link, 'pending', 0, :text, :fmt, "
        "now() + make_interval(secs => :secs), :key, now(), now())",
        id=outbox_id,
        link=link_id,
        text=text,
        fmt=fmt,
        secs=expires_in.total_seconds(),
        key=key,
    )
    return outbox_id


async def claim_kinds(session: Any, key: Any, kinds: list[str] | None, limit: int = 50) -> Any:
    service = outbox_service(session)
    return await (service.claim(key.id, limit) if kinds is None else service.claim(key.id, limit, kinds=kinds))


async def claim_notify(session: Any, key: Any, limit: int = 50) -> Any:
    """Бот просит строки `notify` (по умолчанию бот 0.2.0 их не видит)."""
    return await outbox_service(session).claim(key.id, limit, kinds=["notify"])


# --- HTTP -----------------------------------------------------------------------------------------------------------


async def http_claim_kinds(bot: httpx.AsyncClient, kinds: list[str] | None, limit: int = 50) -> httpx.Response:
    body: dict[str, Any] = {"limit": limit}
    if kinds is not None:
        body["kinds"] = kinds
    return await bot.post(OUTBOX_CLAIM_URL, json=body)


async def http_heartbeat(bot: httpx.AsyncClient, *, local: bool | None = None, name: str = "tollwire_bot") -> Any:
    body: dict[str, Any] = {"bot_username": name, "version": "0.3.0"}
    if local is not None:
        body["local"] = local
    return await bot.post(BOT_HEARTBEAT_URL, json=body)


async def http_bind(
    bot: httpx.AsyncClient, code: str, telegram_id: int = TG_ADMIN, username: str | None = "admin_tg"
) -> httpx.Response:
    body: dict[str, Any] = {"code": code, "telegram_id": telegram_id}
    if username is not None:
        body["telegram_username"] = username
    return await bot.post(BOT_ADMIN_LINKS_URL, json=body)


async def http_issue_code(api: httpx.AsyncClient) -> dict[str, Any]:
    response = await api.post(ME_CODE_URL)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def http_link_admin(api: httpx.AsyncClient, bot: httpx.AsyncClient, telegram_id: int = TG_ADMIN) -> str:
    """Админ получает код в панели, бот привязывает по нему Telegram. Возвращает код."""
    code = (await http_issue_code(api))["code"]
    response = await http_bind(bot, code, telegram_id)
    assert response.status_code == 200, response.text
    return str(code)


async def ack_sent(bot: httpx.AsyncClient, claimed: dict[str, Any]) -> httpx.Response:
    return await http_ack(bot, claimed["lease_id"], [{"id": item["id"], "status": "sent"} for item in claimed["items"]])


Step = Callable[[], Awaitable[Any]]
