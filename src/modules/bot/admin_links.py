from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.exceptions import InvalidDataError, NotFoundError, ReasonConflictError
from core.security import generate_token, hash_token
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction
from modules.audit.service import AuditService
from modules.bot.admin_link_repository import AdminLinkRepository
from modules.bot.models.admin_link import AdminTelegramLink
from modules.bot.models.outbox import PRIORITY_NOTIFY, BotOutbox, OutboxKind
from modules.bot.notify_catalog import CATALOG, default_events
from modules.bot.repository import BotKeyRepository
from modules.bot.schemas import AdminLinkCodeCreated
from modules.bot.telegram_html import TelegramHtmlError, check_telegram_html


@dataclass(frozen=True)
class AdminLinkResult:
    admin_name: str


class AdminLinkService:
    """Owns link transactions; codes and Telegram identities never enter the audit log."""

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.links = AdminLinkRepository(session)
        self.audit = AuditService(session, actor)

    async def issue_code(self, admin_id: UUID) -> tuple[str, datetime]:
        # Lock the parent even before the first link exists, serializing first-time issuance.
        admin = await self.links.lock_admin(admin_id)
        if admin is None or not admin.is_active:
            raise NotFoundError("Active admin not found")
        link = await self.links.get(admin_id)
        if link is None:
            link = AdminTelegramLink(admin_id=admin_id, events=default_events())
            self.session.add(link)
        code = generate_token(24)  # 24 bytes produce 32 URL-safe characters.
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.bot_admin_link_code_ttl_seconds)
        link.code_hash = hash_token(code)
        link.code_expires_at = expires_at
        await self.session.flush()
        self.audit.record(AuditAction("bot_admin_link_code_created"), "admin_telegram_link", link.id)
        await self.session.commit()
        return code, expires_at

    async def link(self, code: str, telegram_id: int, telegram_username: str | None = None) -> AdminLinkResult:
        code_hash = hash_token(code)
        admin_id = await self.links.admin_for_code(code_hash)
        if admin_id is None:
            raise ReasonConflictError("link_invalid")
        # Same lock order as issuance/unlink; recheck the code after acquiring the locks.
        admin = await self.links.lock_admin(admin_id)
        link = await self.links.get(admin_id)
        if admin is None or not admin.is_active or link is None or link.code_hash != code_hash:
            raise ReasonConflictError("link_invalid")
        if link.code_expires_at is None or link.code_expires_at <= datetime.now(timezone.utc):
            raise ReasonConflictError("link_expired")
        owner = await self.links.telegram_owner(telegram_id)
        if owner is not None and owner != admin_id:
            raise ReasonConflictError("telegram_taken")
        replaced = link.telegram_id is not None and link.telegram_id != telegram_id
        result = AdminLinkResult(admin_name=admin.email)
        try:
            # The unique constraint also arbitrates two different admins claiming one Telegram.
            async with self.session.begin_nested():
                link.telegram_id = telegram_id
                link.telegram_username = telegram_username
                link.linked_at = datetime.now(timezone.utc)
                link.blocked_at = None
                link.code_hash = None
                link.code_expires_at = None
                await self.session.flush()
        except IntegrityError as exc:
            raise ReasonConflictError("telegram_taken") from exc
        self.audit.record(
            AuditAction("bot_admin_link_created"),
            "admin_telegram_link",
            link.id,
            {"admin_id": admin_id, "replaced": replaced},
        )
        await self.session.commit()
        return result

    async def unlink(self, admin_id: UUID) -> None:
        await self.links.lock_admin(admin_id)
        link = await self.links.get(admin_id)
        if link is not None:
            self.audit.record(AuditAction("bot_admin_link_removed"), "admin_telegram_link", link.id)
            # Queue rows are removed by the admin_link_id ON DELETE CASCADE foreign key.
            await self.session.delete(link)
        await self.session.commit()

    async def set_events(self, admin_id: UUID, events: dict[str, bool]) -> None:
        if any(key not in CATALOG or not isinstance(value, bool) for key, value in events.items()):
            raise InvalidDataError("Unknown notification event or non-boolean value")
        admin = await self.links.lock_admin(admin_id)
        if admin is None or not admin.is_active:
            raise NotFoundError("Active admin not found")
        link = await self.links.get(admin_id)
        if link is None:
            link = AdminTelegramLink(admin_id=admin_id, events={**default_events(), **events})
            self.session.add(link)
        else:
            link.events = {**link.events, **events}
        await self.session.flush()
        self.audit.record(AuditAction("bot_admin_link_events_updated"), "admin_telegram_link", link.id)
        await self.session.commit()

    async def chats(self) -> list[int]:
        return [
            link.telegram_id
            for link in await self.links.live_links()
            if link.telegram_id is not None and link.events.get("panel_down", False)
        ]

    async def create_code(self, admin_id: UUID) -> AdminLinkCodeCreated:
        code, expires_at = await self.issue_code(admin_id)
        username = await BotKeyRepository(self.session).latest_bot_username()
        url = f"https://t.me/{quote(username.lstrip('@'), safe='')}?start={code}" if username else None
        return AdminLinkCodeCreated(code=code, link=url, expires_at=expires_at)

    async def queue_test(self, admin_id: UUID, text: str | None = None) -> BotOutbox:
        """Queue one message to the caller, independent of notification event preferences."""
        if text is not None:
            try:
                check_telegram_html(text)
            except TelegramHtmlError as exc:
                raise InvalidDataError(str(exc)) from exc
        admin = await self.links.lock_admin(admin_id)
        link = await self.links.get(admin_id)
        if admin is None or not admin.is_active or link is None or link.telegram_id is None:
            raise ReasonConflictError("not_linked")
        if link.blocked_at is not None:
            raise ReasonConflictError("bot_blocked")
        row = BotOutbox(
            kind=OutboxKind.NOTIFY.value,
            admin_link_id=link.id,
            text=text if text is not None else "Уведомления Tollwire работают.",
            format="html" if text is not None else "plain",
            priority=PRIORITY_NOTIFY,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.bot_notify_ttl_hours),
        )
        self.session.add(row)
        await self.session.flush()
        self.audit.record(AuditAction.BOT_NOTIFY_TEST_QUEUED, "admin_telegram_link", link.id)
        await self.session.commit()
        return row
