#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from collections.abc import Sequence
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import InvalidDataError, NotFoundError
from core.security import generate_token, hash_token
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.bot.models.invite import BotInvite
from modules.bot.repository import BotInviteRepository, BotKeyRepository
from modules.bot.schemas import InviteCreate
from modules.protocols.repository import ProtocolRepository

# 18 байт = 24 символа из A-Za-z0-9_-: укладывается в параметр start ссылки Telegram (до 64 символов).
CODE_BYTES = 18


class InviteStatus(enum.Enum):
    VALID = "valid"
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNKNOWN = "unknown"  # нет такого кода: у приглашения в БД этого статуса не бывает


def generate_code() -> str:
    return generate_token(CODE_BYTES)


def invite_status(invite: BotInvite, now: datetime) -> InviteStatus:
    """Использованное остаётся использованным и после срока: бот скажет человеку верную причину."""
    if invite.used_at is not None:
        return InviteStatus.USED
    if invite.revoked_at is not None:
        return InviteStatus.REVOKED
    if invite.expires_at <= now:  # срок «до» не включает сам момент
        return InviteStatus.EXPIRED
    return InviteStatus.VALID


class BotInviteService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.invites = BotInviteRepository(session)
        self.protocols = ProtocolRepository(session)
        self.keys = BotKeyRepository(session)
        self.audit = AuditService(session, actor)
        self.actor = actor

    async def create(self, data: InviteCreate, *, source: str | None = None) -> tuple[BotInvite, str]:
        """Новое приглашение и его открытый код: показывается один раз, в БД остаётся только хеш.

        source - откуда выпущено (cli - консоль на сервере панели); попадает в аудит."""
        if not await self.protocols.get_by_id(data.protocol_id):
            raise NotFoundError(f"Protocol '{data.protocol_id}' not found")
        if data.expires_at <= datetime.now(timezone.utc):
            raise InvalidDataError("Invite deadline must be in the future")
        code = generate_code()
        invite = await self.invites.create(
            BotInvite(
                code_hash=hash_token(code),
                protocol_id=data.protocol_id,
                days=data.days,
                expires_at=data.expires_at,
                note=data.note,
                created_by=self.actor.admin_id,
            )
        )
        self.audit.record(
            AuditAction.BOT_INVITE_CREATED,
            AuditEntity.BOT_INVITE,
            invite.id,
            {
                "protocol_id": invite.protocol_id,
                "days": invite.days,
                "expires_at": invite.expires_at,
                **({"source": source} if source else {}),
            },
        )
        await self.session.commit()
        return invite, code

    async def revoke(self, invite_id: UUID) -> BotInvite:
        invite = await self.invites.get_by_id(invite_id)
        if invite is None:
            raise NotFoundError(f"Invite '{invite_id}' not found")
        if invite.revoked_at is None:
            invite.revoked_at = datetime.now(timezone.utc)
            self.audit.record(AuditAction.BOT_INVITE_REVOKED, AuditEntity.BOT_INVITE, invite.id)
        await self.session.commit()
        return invite

    async def list(self) -> Sequence[BotInvite]:
        return await self.invites.list_all()

    async def check(self, code: str) -> InviteStatus:
        """Состояние приглашения для бота. Ничего не меняет и никаких подробностей приглашения не раскрывает."""
        invite = await self.invites.get_by_hash(hash_token(code))
        if invite is None:
            return InviteStatus.UNKNOWN
        return invite_status(invite, datetime.now(timezone.utc))

    async def link(self, code: str) -> str | None:
        """Ссылка t.me/<бот>?start=<код>: имя бота известно, когда он хоть раз прислал heartbeat."""
        username = await self.keys.latest_bot_username()
        return f"https://t.me/{quote(username.lstrip('@'), safe='')}?start={quote(code, safe='')}" if username else None
