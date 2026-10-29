#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Чтение для страницы «Бот» в панели: состояние, получатели, протоколы, списки. Записей здесь нет."""

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.exceptions import NotFoundError
from modules.bot.models.broadcast import BotBroadcast
from modules.bot.models.invite import BotInvite
from modules.bot.models.registration import BotRegistration, RegistrationStatus
from modules.bot.repository import BotPageRepository, OutboxRepository
from modules.bot.schemas import (
    BotProtocolRead,
    BotStatusRead,
    BroadcastRead,
    OutboxCounts,
    OutboxRowRead,
    RecipientRead,
)

Cursor = tuple[datetime, UUID] | None


def broadcast_read(broadcast: BotBroadcast, has_open_rows: bool) -> BroadcastRead:
    """Состояние: отменена; идёт, пока есть ждущие или выданные строки; иначе завершена (в том числе когда строки
    пропали вместе с удалёнными людьми и finished_at никто не поставил)."""
    state: Literal["running", "cancelled", "finished"]
    if broadcast.cancelled_at is not None:
        state = "cancelled"
    elif broadcast.finished_at is not None or not has_open_rows:
        state = "finished"
    else:
        state = "running"
    return BroadcastRead(
        id=broadcast.id,
        kind=broadcast.kind,
        text=broadcast.text,
        target=broadcast.target,
        protocol_id=broadcast.protocol_id,
        created_at=broadcast.created_at,
        cancelled_at=broadcast.cancelled_at,
        finished_at=broadcast.finished_at,
        total=broadcast.total,
        sent=broadcast.sent,
        blocked=broadcast.blocked,
        failed=broadcast.failed,
        skipped=broadcast.skipped,
        cancelled=broadcast.cancelled,
        state=state,
    )


class BotPageService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.page = BotPageRepository(session)

    async def status(self) -> BotStatusRead:
        keys = list(await self.page.keys())
        active = [key for key in keys if key.revoked_at is None]
        # Состояние бота - по последнему замеченному действующему ключу.
        heard = sorted(
            ((key.last_seen_at, key) for key in active if key.last_seen_at is not None), key=lambda pair: pair[0]
        )
        seen = heard[-1][1] if heard else None
        claims = [key.last_claim_at for key in active if key.last_claim_at is not None]
        last_seen = seen.last_seen_at if seen else None
        stale = last_seen is None or datetime.now(timezone.utc) - last_seen > timedelta(
            seconds=settings.bot_stale_after_seconds
        )
        pending, claimed = await self.page.outbox_counts()
        return BotStatusRead(
            has_keys=bool(keys),
            active_keys=len(active),
            bot_username=seen.bot_username if seen else None,
            last_seen_at=last_seen,
            last_seen_version=seen.last_seen_version if seen else None,
            stale=stale,
            last_claim_at=max(claims) if claims else None,
            outbox=OutboxCounts(pending=pending, claimed=claimed),
        )

    async def recipients(self, search: str | None, limit: int) -> list[RecipientRead]:
        return [
            RecipientRead(
                id=user.id,
                username=user.username,
                telegram_username=user.telegram_username,
                bot_blocked_at=user.bot_blocked_at,
            )
            for user in await self.page.recipients(search.strip() if search else None, limit)
        ]

    async def protocols(self) -> list[BotProtocolRead]:
        return [
            BotProtocolRead(id=protocol.id, server_id=protocol.server_id, server_name=name, type=protocol.type.value)
            for protocol, name in await self.page.protocols()
        ]

    async def invites(self, limit: int, before: Cursor) -> Sequence[tuple[BotInvite, UUID | None, str | None]]:
        return await self.page.invites(limit, before)

    async def registrations(
        self, status: RegistrationStatus | None, limit: int, before: Cursor
    ) -> Sequence[BotRegistration]:
        return await self.page.registrations(status, limit, before)

    async def broadcasts(self, limit: int, before: Cursor) -> list[BroadcastRead]:
        found = await self.page.broadcasts(limit, before)
        open_ids = await self.page.open_broadcast_ids(broadcast.id for broadcast in found)
        return [broadcast_read(broadcast, broadcast.id in open_ids) for broadcast in found]

    async def read(self, broadcast: BotBroadcast) -> BroadcastRead:
        open_ids = await self.page.open_broadcast_ids([broadcast.id])
        return broadcast_read(broadcast, broadcast.id in open_ids)

    async def broadcast(self, broadcast_id: UUID) -> BroadcastRead:
        found = await OutboxRepository(self.session).get_broadcast(broadcast_id)
        if found is None:
            raise NotFoundError(f"Broadcast '{broadcast_id}' not found")
        return await self.read(found)

    async def outbox_row(self, outbox_id: UUID) -> OutboxRowRead:
        row = await OutboxRepository(self.session).get_row(outbox_id)
        if row is None:
            raise NotFoundError(f"Message '{outbox_id}' not found")
        return OutboxRowRead(id=row.id, status=row.status, error=row.error)
