"""Transactional audit-to-outbox collector with overlap for late commits."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from modules.audit.models.audit_log import AuditLog
from modules.audit.repository import AuditLogRepository
from modules.auth.models.admin import Admin
from modules.bot.models.admin_link import AdminTelegramLink
from modules.bot.models.notify import BotNotifyCursor, BotNotifySeen
from modules.bot.models.outbox import PRIORITY_NOTIFY, BotOutbox, OutboxKind
from modules.bot.models.registration import BotRegistration
from modules.bot.notify_catalog import CATALOG, event_keys
from modules.bot.notify_texts import event_line, render_notification
from modules.servers.models.server import VPNServer


class NotifyCollector:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def run(self) -> int:
        try:
            queued = await self._collect()
            await self.session.commit()
            return queued
        except BaseException:
            await self.session.rollback()
            raise

    async def _collect(self) -> int:
        # Fixed namespace/key pair, independent of process hash randomization.
        await self.session.execute(text("SELECT pg_advisory_xact_lock(25425, 5)"))
        now = datetime.now(timezone.utc)
        cursor = await self.session.scalar(
            select(BotNotifyCursor)
            .where(BotNotifyCursor.id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if cursor is None:
            self.session.add(BotNotifyCursor(id=1, cursor_at=now, started_at=now))
            return 0
        overlap = timedelta(seconds=settings.bot_notify_overlap_seconds)
        since = max(cursor.cursor_at - overlap, cursor.started_at)
        actions = tuple(dict.fromkeys(action for event in CATALOG.values() for action in event.actions))
        logs = await AuditLogRepository(self.session).list_for_notify(
            actions, since, select(BotNotifySeen.audit_log_id), settings.bot_notify_batch
        )
        links = (
            await self.session.scalars(
                select(AdminTelegramLink)
                .join(Admin, Admin.id == AdminTelegramLink.admin_id)
                .where(
                    Admin.is_active.is_(True),
                    AdminTelegramLink.telegram_id.is_not(None),
                    AdminTelegramLink.blocked_at.is_(None),
                )
            )
        ).all()
        grouped: dict[str, list[AuditLog]] = defaultdict(list)
        for log in logs:
            self.session.add(BotNotifySeen(audit_log_id=log.id, seen_at=now))
            if log.created_at + timedelta(hours=settings.bot_notify_ttl_hours) <= now:
                continue
            for key in event_keys(log.action.value, log.meta):
                grouped[key].append(log)
        names = await self._server_names(logs) if links and grouped else {}
        queued = 0
        for link in links:
            for key, events in grouped.items():
                if link.events.get(key) is not True:
                    continue
                batches = [events] if len(events) > 3 else [[event] for event in events]
                for batch in batches:
                    entities = {(event.entity_type, event.entity_id) for event in batch}
                    entity_type, entity_id = next(iter(entities)) if len(entities) == 1 else (None, None)
                    lines = [
                        event_line(
                            self._name(event, names),
                            event.action.value,
                            event.created_at,
                            settings.bot_notify_timezone,
                            (event.meta or {}).get("reason") if key == "server_down" else None,
                        )
                        for event in batch
                    ]
                    self.session.add(
                        BotOutbox(
                            kind=OutboxKind.NOTIFY,
                            admin_link_id=link.id,
                            user_id=None,
                            priority=PRIORITY_NOTIFY,
                            text=render_notification(
                                key,
                                lines,
                                settings.public_base_url,
                                since=min(event.created_at for event in batch),
                                until=max(event.created_at for event in batch),
                                entity_type=entity_type,
                                entity_id=entity_id,
                            ),
                            format="plain",
                            notify_key=key,
                            expires_at=min(event.created_at for event in batch)
                            + timedelta(hours=settings.bot_notify_ttl_hours),
                        )
                    )
                    queued += 1
        if logs:
            cursor.cursor_at = max(cursor.cursor_at, max(log.created_at for log in logs))
        # Keep deduplication for anything still inside the read window, even when the cursor is idle.
        retained = select(AuditLog.id).where(AuditLog.created_at >= max(cursor.cursor_at - overlap, cursor.started_at))
        await self.session.execute(
            delete(BotNotifySeen).where(
                BotNotifySeen.seen_at < now - 2 * overlap, BotNotifySeen.audit_log_id.not_in(retained)
            )
        )
        return queued

    @staticmethod
    def _name(event: AuditLog, names: dict[UUID, str]) -> str:
        if event.entity_id is None:
            return "событие"
        kind = "сервер" if event.entity_type == "server" else "объект"
        return names.get(event.entity_id, f"{kind} {str(event.entity_id)[:8]}")

    async def _server_names(self, logs: Sequence[AuditLog]) -> dict[UUID, str]:
        ids = {log.entity_id for log in logs if log.entity_type == "server" and log.entity_id is not None}
        names: dict[UUID, str] = {}
        if ids:
            rows = await self.session.execute(select(VPNServer.id, VPNServer.name).where(VPNServer.id.in_(ids)))
            names.update(rows.tuples().all())
        registrations = {log.entity_id for log in logs if log.entity_type == "bot_registration" and log.entity_id}
        if registrations:
            rows = await self.session.execute(
                select(BotRegistration.id, BotRegistration.name).where(BotRegistration.id.in_(registrations))
            )
            names.update(rows.tuples().all())
        return names
