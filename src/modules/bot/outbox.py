#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Исходящая очередь бота и рассылки: постановка, выдача пачки боту (claim), подтверждение (ack), отмена.

Темп отправки панель не держит: лимит Telegram принадлежит токену бота (Р2-A). Панель выдаёт пачки с арендой
(`lease_id`): подтверждение принимается только от того, кому выдали. Ссылка на конфиг выпускается в момент выдачи
сообщения боту и живёт лишь в ответе claim - в очереди, рассылках и аудите токена нет (Р1-A)."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.exceptions import InvalidDataError, NotFoundError, ReasonConflictError
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.bot.config_links import CONFIG_UNAVAILABLE, NO_ACCESS, NOT_CONFIGURED, NOT_READY, ConfigLinkService
from modules.bot.models.broadcast import BotBroadcast
from modules.bot.models.outbox import PRIORITY_INDIVIDUAL, BotOutbox, OutboxKind, OutboxStatus
from modules.bot.repository import OutboxRepository
from modules.bot.schemas import AudienceRequest, BroadcastCreate, OutboxAckItem
from modules.vpn_users.repository import VPNUserRepository

logger = logging.getLogger("bot")

NO_TELEGRAM = "no_telegram"  # причина 409 и код строки: у человека нет привязанного Telegram
BOT_BLOCKED = "bot_blocked"  # причина 409: человек заблокировал бота
TOO_MANY_ATTEMPTS = "too_many_attempts"
NOT_READY_RETRY_SECONDS = 60  # клиента ещё выдают: строку возвращаем в очередь на минуту
NOT_READY_GIVE_UP = timedelta(minutes=30)  # дольше строка ждёт клиента - пропускается
RETRY_MIN_SECONDS = 5


@dataclass(frozen=True)
class Audience:
    recipients: int
    without_telegram: int
    blocked: int


@dataclass(frozen=True)
class ClaimItem:
    id: UUID
    kind: str
    telegram_id: int
    broadcast_id: UUID | None
    # Адрес с токеном: в repr и лог не попадает.
    config_url: str | None = field(default=None, repr=False)
    valid_until: datetime | None = None


@dataclass(frozen=True)
class ClaimResult:
    lease_id: UUID
    lease_until: datetime
    items: list[ClaimItem]
    texts: dict[UUID, str]


@dataclass(frozen=True)
class AckResult:
    applied: list[UUID]
    ignored: list[UUID]


@dataclass(frozen=True)
class _Handed:
    """Выданная строка простыми значениями (объекты сессии после rollback'а читать нельзя)."""

    id: UUID
    kind: str
    telegram_id: int
    user_id: UUID
    grant_id: UUID | None
    broadcast_id: UUID | None
    created_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OutboxService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.outbox = OutboxRepository(session)
        self.users = VPNUserRepository(session)
        self.links = ConfigLinkService(session, actor)
        self.audit = AuditService(session, actor)

    # --- рассылки --------------------------------------------------------------------------------------------------

    async def audience(self, data: AudienceRequest) -> Audience:
        recipients, without_telegram, blocked = await self.outbox.audience(data, _now())
        return Audience(recipients, without_telegram, blocked)

    async def create_broadcast(self, data: BroadcastCreate) -> BotBroadcast:
        """Рассылка и её строки в очереди. Повторный запрос с тем же request_id возвращает ту же рассылку."""
        existing = await self.outbox.get_broadcast_by_request(data.request_id)
        if existing is not None:
            return existing
        if data.kind == "config" and settings.public_base_url is None:
            raise ReasonConflictError(NOT_CONFIGURED)
        now = _now()
        recipients, _, _ = await self.outbox.audience(data, now)
        if recipients > settings.bot_broadcast_max_recipients:
            raise InvalidDataError(f"A broadcast may have at most {settings.bot_broadcast_max_recipients} recipients")
        try:
            broadcast = await self.outbox.create_broadcast(
                BotBroadcast(
                    kind=data.kind,
                    text=data.text if data.kind == "news" else None,
                    target=data.target,
                    protocol_id=data.protocol_id if data.target == "protocol" else None,
                    request_id=data.request_id,
                    created_by=self.actor.admin_id,
                )
            )
        except IntegrityError:  # тот же request_id пришёл одновременно: рассылка уже создана другим запросом
            await self.session.rollback()
            existing = await self.outbox.get_broadcast_by_request(data.request_id)
            if existing is None:
                raise
            return existing
        broadcast.total = await self.outbox.queue_broadcast(broadcast, data, now)
        meta: dict[str, object] = {"kind": data.kind, "target": data.target, "recipients": broadcast.total}
        if data.target == "protocol":
            meta["protocol_id"] = data.protocol_id
        if data.kind == "news":
            meta["text_length"] = len(data.text or "")  # только длина: текста в журнале нет
        self.audit.record(AuditAction.BOT_BROADCAST_CREATED, AuditEntity.BOT_BROADCAST, broadcast.id, meta)
        if broadcast.total == 0:
            await self.outbox.finish_if_done([broadcast.id], now)
        await self.session.commit()
        return broadcast

    async def cancel_broadcast(self, broadcast_id: UUID) -> BotBroadcast:
        """Ждущие строки закрываются, выданные боту доходят. Повторная отмена ничего не меняет."""
        broadcast = await self.outbox.get_broadcast(broadcast_id)
        if broadcast is None:
            raise NotFoundError(f"Broadcast '{broadcast_id}' not found")
        if broadcast.cancelled_at is not None or broadcast.finished_at is not None:
            return broadcast  # уже отменена или завершена: отменять нечего
        now = _now()
        # Порядок блокировок как у claim и ack: сначала строки очереди, потом рассылка (иначе взаимная блокировка).
        cancelled = await self.outbox.cancel_waiting(broadcast.id, now)
        broadcast = await self.outbox.get_broadcast(broadcast_id, lock=True) or broadcast
        broadcast.cancelled += cancelled
        if broadcast.cancelled_at is None:
            broadcast.cancelled_at = now
            self.audit.record(
                AuditAction.BOT_BROADCAST_CANCELLED, AuditEntity.BOT_BROADCAST, broadcast.id, {"cancelled": cancelled}
            )
        await self.outbox.finish_if_done([broadcast.id], now)
        await self.session.commit()
        return broadcast

    # --- ссылка одному человеку ------------------------------------------------------------------------------------

    async def queue_config_link(self, user_id: UUID) -> BotOutbox:
        """«Отправить ссылку в Telegram» из карточки: строка с высшим приоритетом. Если открытая отправка ссылки этому
        человеку уже есть (в том числе из рассылки), возвращается она."""
        user = await self.outbox.fresh_user(user_id)
        if user is None:
            raise NotFoundError("User not found")
        if user.telegram_id is None:
            raise ReasonConflictError(NO_TELEGRAM)
        if user.bot_blocked_at is not None:
            raise ReasonConflictError(BOT_BLOCKED)
        try:
            await self.links.ensure_issuable(user.id)
        except ReasonConflictError as refusal:
            if refusal.reason != NOT_READY:  # клиента ещё выдают: ставим, при выдаче строка подождёт
                raise
        existing = await self.outbox.open_config_row(user.id)
        if existing is not None:
            if existing.broadcast_id is not None:
                # Ссылка уже ждёт в рассылке: просьба админа попадает в аудит, строка остаётся прежней.
                self.audit.record(
                    AuditAction.BOT_CONFIG_LINK_QUEUED, AuditEntity.VPN_USER, user.id, {"outbox_id": existing.id}
                )
                await self.session.commit()
            return existing
        try:
            row = await self.outbox.add_row(
                BotOutbox(kind=OutboxKind.CONFIG.value, user_id=user.id, priority=PRIORITY_INDIVIDUAL)
            )
        except IntegrityError:  # две отправки одновременно: открытая строка уже поставлена другой
            await self.session.rollback()
            existing = await self.outbox.open_config_row(user.id)
            if existing is None:
                raise
            return existing
        self.audit.record(AuditAction.BOT_CONFIG_LINK_QUEUED, AuditEntity.VPN_USER, user.id, {"outbox_id": row.id})
        await self.session.commit()
        return row

    # --- выдача пачки боту -----------------------------------------------------------------------------------------

    async def claim(self, key_id: UUID, limit: int) -> ClaimResult:
        now = _now()
        lease_id = uuid4()
        lease_until = now + timedelta(seconds=settings.bot_outbox_lease_seconds)
        await self._touch_key(key_id, now)
        rows = await self.outbox.lease_candidates(now, limit)
        users = await self.outbox.users_by_ids({row.user_id for row in rows})
        broadcasts = await self.outbox.broadcasts({row.broadcast_id for row in rows if row.broadcast_id})

        closed: defaultdict[UUID, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
        handed: list[BotOutbox] = []
        for row in rows:
            user = users.get(row.user_id)
            broadcast = broadcasts.get(row.broadcast_id) if row.broadcast_id else None
            attempts = row.attempts + (1 if row.status == OutboxStatus.CLAIMED.value else 0)
            if broadcast is not None and broadcast.cancelled_at is not None:
                verdict: tuple[str, str | None] | None = (OutboxStatus.CANCELLED.value, None)
            elif attempts >= settings.bot_outbox_max_attempts:
                verdict = (OutboxStatus.FAILED.value, TOO_MANY_ATTEMPTS)
            elif user is None or user.telegram_id is None:
                verdict = (OutboxStatus.SKIPPED.value, NO_TELEGRAM)
            elif user.bot_blocked_at is not None:
                verdict = (OutboxStatus.BLOCKED.value, "blocked")
            else:
                verdict = None
            if verdict is not None:
                row.status = verdict[0]
                row.error = verdict[1]
                row.lease_id = row.claimed_until = None
                if row.broadcast_id:
                    closed[row.broadcast_id][verdict[0]] += 1
                continue
            row.status = OutboxStatus.CLAIMED.value
            row.lease_id = lease_id
            row.claimed_until = lease_until
            row.claimed_by_key_id = key_id
            row.attempts = attempts
            handed.append(row)
        for broadcast_id, counts in closed.items():
            await self.outbox.add_counts(broadcast_id, **counts)
        await self.outbox.finish_if_done(closed, now)
        # Всё нужное - простыми значениями до commit: дальше возможны rollback'и отказавших строк, после которых
        # объекты сессии обновляются из БД и в async-коде читаться не должны.
        plan = [
            _Handed(
                row.id,
                row.kind,
                users[row.user_id].telegram_id or 0,
                row.user_id,
                row.access_grant_id,
                row.broadcast_id,
                row.created_at,
            )
            for row in handed
        ]
        all_texts = {broadcast_id: broadcast.text or "" for broadcast_id, broadcast in broadcasts.items()}
        await self.session.commit()

        items: list[ClaimItem] = []
        for entry in plan:
            if entry.kind != OutboxKind.CONFIG.value:
                items.append(ClaimItem(entry.id, entry.kind, entry.telegram_id, entry.broadcast_id))
                continue
            item = await self._issue_config(entry)
            if item is not None:
                items.append(item)
        texts = {
            broadcast_id: all_texts[broadcast_id]
            for broadcast_id in {item.broadcast_id for item in items if item.kind == OutboxKind.NEWS.value}
            if broadcast_id is not None and broadcast_id in all_texts
        }
        return ClaimResult(lease_id, lease_until, items, texts)

    async def _touch_key(self, key_id: UUID, now: datetime) -> None:
        from modules.bot.repository import BotKeyRepository

        key = await BotKeyRepository(self.session).get_by_id(key_id)
        if key is not None:
            key.last_claim_at = now

    async def _issue_config(self, entry: "_Handed") -> ClaimItem | None:
        """Новая ссылка для строки `config`, в собственной транзакции: отказ одной строки не держит остальные."""
        try:
            link, token = await self.links.stage_issue(
                entry.user_id,
                entry.grant_id,
                source="outbox",
                meta={"broadcast_id": entry.broadcast_id},
            )
            row = await self.outbox.get_row(entry.id)
            if row is None:  # строку убрали (человека удалили) между выдачей и выпуском
                await self.session.commit()
                return None
            row.config_link_id = link.id
            page = await self.links.url_of(link, token)
            await self.session.commit()
        except ReasonConflictError as refusal:
            await self.session.commit()  # отказ до всяких записей: отпускаем блокировки (rollback обновил бы объекты)
            await self._refuse_config(entry, refusal.reason)
            return None
        except NotFoundError:
            await self.session.commit()
            await self._refuse_config(entry, NO_ACCESS)
            return None
        return ClaimItem(
            entry.id,
            OutboxKind.CONFIG.value,
            entry.telegram_id,
            entry.broadcast_id,
            config_url=page.config_url,
            valid_until=page.valid_until,
        )

    async def _refuse_config(self, entry: "_Handed", reason: str | None) -> None:
        """Ссылку выпустить нельзя: нет доступа, клиент импортирован, адрес панели не задан - строка пропускается;
        клиента ещё выдают - строка на минуту возвращается в очередь (но не дольше NOT_READY_GIVE_UP)."""
        row = await self.outbox.get_row(entry.id)
        if row is None:
            return
        now = _now()
        if reason == NOT_READY and now - entry.created_at < NOT_READY_GIVE_UP:
            row.status = OutboxStatus.PENDING.value
            row.lease_id = row.claimed_until = None
            row.not_before = now + timedelta(seconds=NOT_READY_RETRY_SECONDS)
            await self.session.commit()
            return
        code = reason if reason in (NO_ACCESS, CONFIG_UNAVAILABLE, NOT_CONFIGURED, NOT_READY) else NO_ACCESS
        row.status = OutboxStatus.SKIPPED.value
        row.error = code
        row.lease_id = row.claimed_until = None
        if entry.broadcast_id:
            await self.outbox.add_counts(entry.broadcast_id, skipped=1)
            await self.outbox.finish_if_done([entry.broadcast_id], now)
        await self.session.commit()

    # --- подтверждение ---------------------------------------------------------------------------------------------

    async def ack(self, lease_id: UUID, items: list[OutboxAckItem]) -> AckResult:
        """Итоги отправки от бота. Принимаются только строки этой аренды в статусе claimed (аренда могла истечь, но
        пока строку никому не выдали - итог принимается); остальное - в ignored. Повторный ack ничего не меняет."""
        wanted: dict[UUID, OutboxAckItem] = {}
        for item in items:
            wanted.setdefault(item.id, item)
        rows = await self.outbox.leased_rows(lease_id, wanted)
        now = _now()
        counts: defaultdict[UUID, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
        blocked_users: set[UUID] = set()
        for row_id, row in rows.items():
            item = wanted[row_id]
            if item.status == "sent":
                row.status, row.sent_at, row.error = OutboxStatus.SENT.value, now, None
                outcome = "sent"
            elif item.status == "blocked":
                row.status, row.error = OutboxStatus.BLOCKED.value, "blocked"
                blocked_users.add(row.user_id)
                outcome = "blocked"
            elif item.status == "failed":
                row.status, row.error = OutboxStatus.FAILED.value, item.error or "bad_request"
                outcome = "failed"
            else:  # retry: строка возвращается в очередь не раньше срока; flood попытку не тратит
                row.status = OutboxStatus.PENDING.value
                row.not_before = now + timedelta(seconds=max(item.retry_after or 0, RETRY_MIN_SECONDS))
                if item.error != "flood":
                    row.attempts += 1
                row.lease_id = row.claimed_until = None
                continue
            if row.broadcast_id:
                counts[row.broadcast_id][outcome] += 1
        if blocked_users:
            for user in (await self.outbox.users_by_ids(blocked_users)).values():
                user.bot_blocked_at = now
        for broadcast_id, delta in counts.items():
            await self.outbox.add_counts(broadcast_id, **delta)
        await self.outbox.finish_if_done(counts, now)
        await self.session.commit()
        applied = [item_id for item_id in wanted if item_id in rows]
        ignored = [item_id for item_id in wanted if item_id not in rows]
        return AckResult(applied, ignored)
