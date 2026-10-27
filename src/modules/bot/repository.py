#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.access.models import CredentialSource
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.bot.models.config_link import ConfigLink
from modules.bot.models.invite import BotInvite
from modules.bot.models.key import BotKey
from modules.bot.models.registration import OPEN_STATUSES, BotRegistration, RegistrationStatus
from modules.servers.models.server import VPNServer
from modules.vpn_users.models.user import VPNUser


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


class ConfigLinkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, link: ConfigLink) -> ConfigLink:
        self.session.add(link)
        await self.session.flush()
        return link

    async def credentials_of_user(self, user_id: UUID) -> list[tuple[VPNCredential, AccessGrant, Subscription]]:
        """Не отозванные клиенты человека вместе с доступом и подпиской (годность доступа решает вызывающий)."""
        query = (
            select(VPNCredential, AccessGrant, Subscription)
            .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .where(Subscription.user_id == user_id, VPNCredential.revoked_at.is_(None))
            .execution_options(populate_existing=True)
        )
        rows = (await self.session.execute(query)).all()
        return [(credential, grant, subscription) for credential, grant, subscription in rows]

    async def grants_of_user(self, user_id: UUID) -> list[tuple[AccessGrant, Subscription]]:
        query = (
            select(AccessGrant, Subscription)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .where(Subscription.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        return [(grant, subscription) for grant, subscription in (await self.session.execute(query)).all()]

    async def registered_grant_id(self, telegram_id: int) -> UUID | None:
        """Доступ последней завершённой регистрации через бота этого человека (его клиент важнее более нового)."""
        query = (
            select(BotRegistration.access_grant_id)
            .where(
                BotRegistration.telegram_id == telegram_id,
                BotRegistration.status == RegistrationStatus.ACTIVE,
                BotRegistration.access_grant_id.is_not(None),
            )
            .order_by(BotRegistration.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def lock_credential(self, credential_id: UUID) -> VPNCredential | None:
        """Клиент под блокировкой строки до конца транзакции, со свежими данными из БД."""
        query = (
            select(VPNCredential)
            .where(VPNCredential.id == credential_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def live_links(self, credential_id: UUID) -> list[ConfigLink]:
        query = (
            select(ConfigLink)
            .where(ConfigLink.credential_id == credential_id, ConfigLink.revoked_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(query)).scalars())

    async def valid_until_of(self, credential_id: UUID) -> datetime | None:
        """Конец подписки, по которой выдан клиент."""
        query = (
            select(Subscription.valid_until)
            .join(AccessGrant, AccessGrant.subscription_id == Subscription.id)
            .join(VPNCredential, VPNCredential.access_grant_id == AccessGrant.id)
            .where(VPNCredential.id == credential_id)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def open_target(
        self, token_hash: str
    ) -> tuple[ConfigLink, VPNCredential, AccessGrant, Subscription, str, str] | None:
        """Всё для страницы по хешу токена одним запросом: живая ссылка, не отозванный выданный клиент с конфигом, его
        доступ, подписка, имя человека и имя сервера. Годность доступа по датам и статусам решает вызывающий."""
        query = (
            select(ConfigLink, VPNCredential, AccessGrant, Subscription, VPNUser.username, VPNServer.name)
            .join(VPNCredential, ConfigLink.credential_id == VPNCredential.id)
            .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .join(VPNUser, Subscription.user_id == VPNUser.id)
            .join(VPNServer, AccessGrant.server_id == VPNServer.id)
            .where(
                ConfigLink.token_hash == token_hash,
                ConfigLink.revoked_at.is_(None),
                VPNCredential.revoked_at.is_(None),
                VPNCredential.source == CredentialSource.ISSUED,
                VPNCredential.encrypted_config.is_not(None),
            )
            .execution_options(populate_existing=True)
        )
        row = (await self.session.execute(query)).first()
        if row is None:
            return None
        link, credential, grant, subscription, username, server_name = row
        return link, credential, grant, subscription, username, server_name
