#!/usr/bin/python
# -*- coding: utf-8 -*-
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import BotAuthError
from core.rate_limit import RateLimiter
from core.security import hash_token
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_request_ip
from modules.bot.rate_limits import BOT_AUTH_FAILURES_BY_IP, BOT_REGISTRATIONS_BY_KEY, BOT_REQUESTS_BY_KEY
from modules.bot.repository import BotKeyRepository

# auto_error=False: без заголовка схема отдаёт None, отказ формируем сами (одинаковый для любой причины).
bot_key_header = APIKeyHeader(name="X-Bot-Key", auto_error=False, description="Ключ бота")

_REFUSAL = "Invalid bot key"


@dataclass(frozen=True)
class BotContext:
    """Кто обращается к Bot API: ключ (по нему лимиты и аудит), сессия БД и актор аудита (бот - не админ)."""

    key_id: UUID
    ip: str | None
    session: AsyncSession

    @property
    def actor(self) -> AuditActor:
        return AuditActor(admin_id=None, ip=self.ip)

    async def limit_registrations(self) -> None:
        await RateLimiter().hit(BOT_REGISTRATIONS_BY_KEY, str(self.key_id))


async def require_bot_key(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    raw_key: Annotated[str | None, Security(bot_key_header)],
) -> BotContext:
    """Зависимость маршрута Bot API: действующий ключ бота в X-Bot-Key, иначе 401.

    Нет ключа, неверный и отозванный отвечают одинаково. Ключ даёт доступ только к Bot API: админский API
    открывается JWT, и ни один из этих способов входа не подходит к другому.
    """
    limiter = RateLimiter()
    ip = get_request_ip(request) or "unknown"
    await limiter.ensure_not_exceeded(BOT_AUTH_FAILURES_BY_IP, ip)
    key = await BotKeyRepository(session).get_active_by_hash(hash_token(raw_key)) if raw_key else None
    if key is None:
        await limiter.record(BOT_AUTH_FAILURES_BY_IP, ip)
        raise BotAuthError(_REFUSAL)
    await limiter.hit(BOT_REQUESTS_BY_KEY, str(key.id))
    return BotContext(key_id=key.id, ip=get_request_ip(request), session=session)


require_bot_key.bot_key_required = True  # type: ignore[attr-defined]  # для теста «каждый маршрут Bot API закрыт ключом»

BotKeyContext = Annotated[BotContext, Depends(require_bot_key)]
