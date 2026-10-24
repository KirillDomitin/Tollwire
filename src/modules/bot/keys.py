#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from core.security import generate_token, hash_token
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.bot.models.key import BotKey
from modules.bot.repository import BotKeyRepository
from modules.bot.schemas import BotKeyCreate, HeartbeatRequest

# Префикс по которому ключ бота узнаётся в тексте (core.log маскирует его, даже если имени поля рядом нет).
KEY_PREFIX = "twb_"
KEY_PREFIX_LENGTH = 8  # сколько первых символов ключа хранится открыто: по ним в списке видно, какой это ключ


def generate_key() -> str:
    return KEY_PREFIX + generate_token(32)


class BotKeyService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.keys = BotKeyRepository(session)
        self.audit = AuditService(session, actor)
        self.actor = actor

    async def create(self, data: BotKeyCreate) -> tuple[BotKey, str]:
        """Новый ключ и его открытое значение: оно показывается один раз, в БД остаётся только хеш."""
        secret = generate_key()
        key = await self.keys.create(
            BotKey(
                name=data.name,
                key_hash=hash_token(secret),
                key_prefix=secret[:KEY_PREFIX_LENGTH],
                created_by=self.actor.admin_id,
            )
        )
        self.audit.record(AuditAction.BOT_KEY_CREATED, AuditEntity.BOT_KEY, key.id, {"name": key.name})
        await self.session.commit()
        return key, secret

    async def revoke(self, key_id: UUID) -> BotKey:
        key = await self.keys.get_by_id(key_id, lock=True)
        if key is None:
            raise NotFoundError(f"Bot key '{key_id}' not found")
        if key.revoked_at is None:
            key.revoked_at = datetime.now(timezone.utc)
            self.audit.record(AuditAction.BOT_KEY_REVOKED, AuditEntity.BOT_KEY, key.id, {"name": key.name})
        await self.session.commit()
        return key

    async def list(self) -> Sequence[BotKey]:
        return await self.keys.list_all()

    async def heartbeat(self, key_id: UUID, data: HeartbeatRequest) -> None:
        """Бот сообщает, что жив: имя бота и версия видны админу на странице «Бот»."""
        key = await self.keys.get_by_id(key_id)
        if key is None:
            raise NotFoundError(f"Bot key '{key_id}' not found")
        key.last_seen_at = datetime.now(timezone.utc)
        key.last_seen_version = data.version
        key.bot_username = data.bot_username
        await self.session.commit()
