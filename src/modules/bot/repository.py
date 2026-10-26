#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.bot.models.invite import BotInvite
from modules.bot.models.key import BotKey
from modules.bot.models.registration import OPEN_STATUSES, BotRegistration, RegistrationStatus


class BotKeyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, key: BotKey) -> BotKey:
        self.session.add(key)
        await self.session.flush()
        return key

    async def get_by_id(self, key_id: UUID, *, lock: bool = False) -> BotKey | None:
        return await self.session.get(BotKey, key_id, with_for_update=lock, populate_existing=lock)

    async def get_by_hash(self, key_hash: str) -> BotKey | None:
        """Ключ по хешу, в том числе отозванный (повторный revoke из консоли не считается ошибкой)."""
        query = select(BotKey).where(BotKey.key_hash == key_hash)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_active_by_hash(self, key_hash: str) -> BotKey | None:
        query = select(BotKey).where(BotKey.key_hash == key_hash, BotKey.revoked_at.is_(None))
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_all(self) -> Sequence[BotKey]:
        return (await self.session.execute(select(BotKey).order_by(BotKey.created_at.desc()))).scalars().all()

    async def latest_bot_username(self) -> str | None:
        """Имя бота, которое сообщил последний замеченный действующий ключ: для ссылки t.me в приглашении."""
        query = (
            select(BotKey.bot_username)
            .where(BotKey.revoked_at.is_(None), BotKey.bot_username.is_not(None))
            .order_by(BotKey.last_seen_at.desc())
            .limit(1)
        )
        return (await self.session.execute(query)).scalar_one_or_none()


class BotInviteRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, invite: BotInvite) -> BotInvite:
        self.session.add(invite)
        await self.session.flush()
        return invite

    async def get_by_id(self, invite_id: UUID) -> BotInvite | None:
        return await self.session.get(BotInvite, invite_id)

    async def get_by_hash(self, code_hash: str, *, lock: bool = False) -> BotInvite | None:
        """lock=True: строка блокируется до конца транзакции (свежие данные из БД): приглашение занимает один запрос."""
        query = select(BotInvite).where(BotInvite.code_hash == code_hash)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_all(self) -> Sequence[BotInvite]:
        return (await self.session.execute(select(BotInvite).order_by(BotInvite.created_at.desc()))).scalars().all()


class BotRegistrationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, registration: BotRegistration) -> BotRegistration:
        self.session.add(registration)
        await self.session.flush()
        return registration

    async def get_by_id(self, registration_id: UUID, *, lock: bool = False) -> BotRegistration | None:
        return await self.session.get(BotRegistration, registration_id, with_for_update=lock, populate_existing=lock)

    async def get_by_invite_id(self, invite_id: UUID) -> BotRegistration | None:
        query = select(BotRegistration).where(BotRegistration.invite_id == invite_id)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_by_grant_id(self, access_grant_id: UUID, *, lock: bool = False) -> BotRegistration | None:
        query = select(BotRegistration).where(BotRegistration.access_grant_id == access_grant_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_open_by_telegram_id(self, telegram_id: int) -> BotRegistration | None:
        query = select(BotRegistration).where(
            BotRegistration.telegram_id == telegram_id, BotRegistration.status.in_(OPEN_STATUSES)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_all(self, status: RegistrationStatus | None = None) -> Sequence[BotRegistration]:
        query = select(BotRegistration).order_by(BotRegistration.created_at.desc())
        if status is not None:
            query = query.where(BotRegistration.status == status)
        return (await self.session.execute(query)).scalars().all()

    async def list_provisioning(self) -> Sequence[BotRegistration]:
        """Регистрации, ждущие выдачи клиента, под блокировкой строк (занятые другой транзакцией пропускаются)."""
        query = (
            select(BotRegistration)
            .where(BotRegistration.status == RegistrationStatus.PROVISIONING)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(query)).scalars().all()
