#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AlreadyExistsError, InactiveAccessError, NotFoundError
from core.security import decrypt_secret, encrypt_secret
from modules.access.models import AccessGrantStatus
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import AccessGrantRepository, SubscriptionRepository, VPNCredentialRepository
from modules.access.schemas import (
    AccessGrantCreate,
    AccessGrantUpdate,
    SubscriptionCreate,
    SubscriptionUpdate,
    VPNCredentialConfigRead,
)
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import ServerRepository
from modules.vpn_users.repository import VPNUserRepository

_TERMINAL_STATUSES = (AccessGrantStatus.EXPIRED, AccessGrantStatus.REVOKED)


class SubscriptionService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.subscriptions = SubscriptionRepository(session)
        self.grants = AccessGrantRepository(session)
        self.users = VPNUserRepository(session)

    async def create(self, user_id: UUID, data: SubscriptionCreate) -> Subscription:
        if not await self.users.get_by_id(user_id):
            raise NotFoundError(f"VPN user '{user_id}' not found")
        if await self.subscriptions.get_active_by_user_id(user_id):
            raise AlreadyExistsError("User already has an active subscription")

        new_subscription = Subscription(user_id=user_id, valid_from=data.valid_from, valid_until=data.valid_until)
        subscription = await self.subscriptions.create(new_subscription)
        await self.session.commit()
        return subscription

    async def get(self, subscription_id: UUID) -> Subscription:
        subscription = await self.subscriptions.get_by_id(subscription_id)
        if not subscription:
            raise NotFoundError(f"Subscription '{subscription_id}' not found")
        return subscription

    async def list_by_user(self, user_id: UUID) -> Sequence[Subscription]:
        return await self.subscriptions.list_by_user_id(user_id)

    async def update(self, subscription_id: UUID, data: SubscriptionUpdate) -> Subscription:
        subscription = await self.get(subscription_id)
        fields = data.model_dump(exclude_unset=True)
        for field, value in fields.items():
            setattr(subscription, field, value)
        await self.session.commit()
        return subscription

    async def revoke(self, subscription_id: UUID) -> Subscription:
        """Отзывает подписку и каскадно — все её ещё не завершённые гранты.

        Идемпотентно: повторный вызов не меняет revoked_at. Гранты в терминальном
        статусе (EXPIRED/REVOKED) не трогаются — их история сохраняется как есть.
        """
        subscription = await self.get(subscription_id)
        if subscription.status != AccessGrantStatus.REVOKED:
            now = datetime.now(timezone.utc)
            subscription.status = AccessGrantStatus.REVOKED
            subscription.revoked_at = now
            for grant in await self.grants.list_by_subscription_id(subscription_id):
                if grant.status not in _TERMINAL_STATUSES:
                    grant.status = AccessGrantStatus.REVOKED
                    grant.revoked_at = now
            await self.session.commit()
        return subscription

    async def delete(self, subscription_id: UUID) -> None:
        subscription = await self.get(subscription_id)
        await self.subscriptions.delete(subscription)
        await self.session.commit()

    async def expire(self, subscription_id: UUID) -> Subscription:
        """Переводит подписку и её гранты в EXPIRED, если valid_until уже наступил.

        Идемпотентно: подписку в терминальном статусе (EXPIRED/REVOKED) не трогает.
        REVOKED имеет приоритет над истечением по времени — явный отзыв не
        переписывается автоматическим переходом.
        """
        subscription = await self.get(subscription_id)
        if subscription.status in _TERMINAL_STATUSES:
            return subscription
        if datetime.now(timezone.utc) < subscription.valid_until:
            return subscription

        subscription.status = AccessGrantStatus.EXPIRED
        for grant in await self.grants.list_by_subscription_id(subscription_id):
            if grant.status not in _TERMINAL_STATUSES:
                grant.status = AccessGrantStatus.EXPIRED
        await self.session.commit()
        return subscription


class AccessGrantService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.grants = AccessGrantRepository(session)
        self.subscriptions = SubscriptionRepository(session)
        self.servers = ServerRepository(session)
        self.protocols = ProtocolRepository(session)

    async def create(self, subscription_id: UUID, data: AccessGrantCreate) -> AccessGrant:
        subscription = await self.subscriptions.get_by_id(subscription_id)
        if not subscription:
            raise NotFoundError(f"Subscription '{subscription_id}' not found")

        protocol = await self.protocols.get_by_id(data.protocol_id)
        if not protocol:
            raise NotFoundError(f"Protocol '{data.protocol_id}' not found or not available")

        conflict = await self.grants.find_active_conflict(subscription.user_id, protocol.server_id, protocol.id)
        if conflict:
            raise AlreadyExistsError("An active access grant for this server/protocol already exists")

        new_grant = AccessGrant(
            subscription_id=subscription_id,
            server_id=protocol.server_id,
            protocol_id=protocol.id,
        )
        grant = await self.grants.create(new_grant)
        await self.session.commit()
        return grant

    async def get(self, access_grant_id: UUID) -> AccessGrant:
        grant = await self.grants.get_by_id(access_grant_id)
        if not grant:
            raise NotFoundError(f"Access grant '{access_grant_id}' not found")
        return grant

    async def list_by_subscription(self, subscription_id: UUID) -> Sequence[AccessGrant]:
        return await self.grants.list_by_subscription_id(subscription_id)

    async def update(self, access_grant_id: UUID, data: AccessGrantUpdate) -> AccessGrant:
        grant = await self.get(access_grant_id)
        fields = data.model_dump(exclude_unset=True)
        for field, value in fields.items():
            setattr(grant, field, value)
        await self.session.commit()
        return grant

    async def revoke(self, access_grant_id: UUID) -> AccessGrant:
        grant = await self.get(access_grant_id)
        if grant.status != AccessGrantStatus.REVOKED:
            grant.status = AccessGrantStatus.REVOKED
            grant.revoked_at = datetime.now(timezone.utc)
            await self.session.commit()
        return grant

    async def delete(self, access_grant_id: UUID) -> None:
        grant = await self.get(access_grant_id)
        await self.grants.delete(grant)
        await self.session.commit()


class VPNCredentialService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.credentials = VPNCredentialRepository(session)
        self.grants = AccessGrantRepository(session)
        self.subscriptions = SubscriptionRepository(session)

    async def issue(self, access_grant_id: UUID, config: str) -> VPNCredential:
        """Сохраняет конфиг клиента в зашифрованном виде для живого гранта.

        Сюда приходит уже собранный конфиг (в 12.2 его будет собирать провайдер).
        Второй живой credential на тот же грант не создаётся.
        """
        await self._ensure_grant_is_live(access_grant_id)
        if await self.credentials.get_live_by_access_grant_id(access_grant_id):
            raise AlreadyExistsError("Access grant already has a live credential")

        credential = VPNCredential(access_grant_id=access_grant_id, encrypted_config=encrypt_secret(config))
        await self.credentials.create(credential)
        await self.session.commit()
        return credential

    async def get(self, credential_id: UUID) -> VPNCredential:
        credential = await self.credentials.get_by_id(credential_id)
        if not credential:
            raise NotFoundError(f"Credential '{credential_id}' not found")
        return credential

    async def list_by_access_grant(self, access_grant_id: UUID) -> Sequence[VPNCredential]:
        return await self.credentials.list_by_access_grant_id(access_grant_id)

    async def reveal_config(self, credential_id: UUID) -> VPNCredentialConfigRead:
        """Единственное место, где конфиг расшифровывается. Не логировать результат."""
        credential = await self.get(credential_id)
        if credential.revoked_at is not None:
            raise InactiveAccessError("Credential is revoked")
        await self._ensure_grant_is_live(credential.access_grant_id)
        # TODO(блок 14): записать в AuditLog факт выдачи конфига (кто, какой credential, когда).
        return VPNCredentialConfigRead(
            id=credential.id,
            access_grant_id=credential.access_grant_id,
            config=decrypt_secret(credential.encrypted_config),
        )

    async def revoke(self, credential_id: UUID) -> VPNCredential:
        credential = await self.get(credential_id)
        if credential.revoked_at is None:
            credential.revoked_at = datetime.now(timezone.utc)
            await self.session.commit()
        return credential

    async def _ensure_grant_is_live(self, access_grant_id: UUID) -> None:
        # Проверяем и грант, и подписку: отзыв/истечение подписки каскадят на гранты,
        # но проверка подписки страхует от рассинхрона (данные до каскада, ручные правки через PATCH).
        grant = await self.grants.get_by_id(access_grant_id)
        if not grant:
            raise NotFoundError(f"Access grant '{access_grant_id}' not found")
        if grant.status in _TERMINAL_STATUSES:
            raise InactiveAccessError(f"Access grant is {grant.status.value}")
        subscription = await self.subscriptions.get_by_id(grant.subscription_id)
        if not subscription or subscription.status in _TERMINAL_STATUSES:
            raise InactiveAccessError("Subscription is not active")
