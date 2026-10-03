#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.access.models import AccessGrantStatus
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential

_ACTIVE_STATUSES = (AccessGrantStatus.PENDING, AccessGrantStatus.ACTIVE)


class SubscriptionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, subscription: Subscription) -> Subscription:
        self.session.add(subscription)
        await self.session.flush()
        return subscription

    async def get_by_id(self, subscription_id: UUID, *, lock: bool = False) -> Subscription | None:
        """lock=True — свежие данные из БД (не из кэша сессии) под блокировкой строки до конца транзакции."""
        return await self.session.get(Subscription, subscription_id, with_for_update=lock, populate_existing=lock)

    async def list_by_user_id(self, user_id: UUID) -> Sequence[Subscription]:
        sql_request = select(Subscription).where(Subscription.user_id == user_id)
        result = await self.session.execute(sql_request)
        return result.scalars().all()

    async def get_active_by_user_id(self, user_id: UUID) -> Subscription | None:
        sql_request = select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.status.in_(_ACTIVE_STATUSES),
        )
        result = await self.session.execute(sql_request)
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[Subscription]:
        sql_request = select(Subscription)
        result = await self.session.execute(sql_request)
        return result.scalars().all()

    async def list_due_for_expiry(self, now: datetime) -> Sequence[UUID]:
        """Ещё не завершённые подписки, у которых valid_until уже наступил."""
        sql_request = select(Subscription.id).where(
            Subscription.status.in_(_ACTIVE_STATUSES), Subscription.valid_until <= now
        )
        result = await self.session.execute(sql_request)
        return result.scalars().all()

    async def list_due_for_activation(self, now: datetime) -> Sequence[UUID]:
        """PENDING-подписки, чей период уже начался и ещё не закончился."""
        sql_request = select(Subscription.id).where(
            Subscription.status == AccessGrantStatus.PENDING,
            Subscription.valid_from <= now,
            Subscription.valid_until > now,
        )
        result = await self.session.execute(sql_request)
        return result.scalars().all()

    async def delete(self, subscription: Subscription) -> None:
        await self.session.delete(subscription)


class AccessGrantRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, access_grant: AccessGrant) -> AccessGrant:
        self.session.add(access_grant)
        await self.session.flush()
        return access_grant

    async def get_by_id(self, access_grant_id: UUID, *, lock: bool = False) -> AccessGrant | None:
        """lock=True — свежие данные из БД (не из кэша сессии) под блокировкой строки до конца транзакции."""
        return await self.session.get(AccessGrant, access_grant_id, with_for_update=lock, populate_existing=lock)

    async def list_by_subscription_id(self, subscription_id: UUID) -> Sequence[AccessGrant]:
        sql_request = select(AccessGrant).where(AccessGrant.subscription_id == subscription_id)
        result = await self.session.execute(sql_request)
        return result.scalars().all()

    async def find_active_conflict(self, user_id: UUID, server_id: UUID, protocol_id: UUID) -> AccessGrant | None:
        sql_request = (
            select(AccessGrant)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .where(
                Subscription.user_id == user_id,
                AccessGrant.server_id == server_id,
                AccessGrant.protocol_id == protocol_id,
                AccessGrant.status.in_(_ACTIVE_STATUSES),
            )
        )
        result = await self.session.execute(sql_request)
        return result.scalar_one_or_none()

    async def delete(self, access_grant: AccessGrant) -> None:
        await self.session.delete(access_grant)


class VPNCredentialRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, credential: VPNCredential) -> VPNCredential:
        self.session.add(credential)
        await self.session.flush()
        return credential

    async def get_by_id(self, credential_id: UUID) -> VPNCredential | None:
        return await self.session.get(VPNCredential, credential_id)

    async def list_by_access_grant_id(self, access_grant_id: UUID) -> Sequence[VPNCredential]:
        sql_request = (
            select(VPNCredential)
            .where(VPNCredential.access_grant_id == access_grant_id)
            .order_by(VPNCredential.created_at.desc())
        )
        result = await self.session.execute(sql_request)
        return result.scalars().all()

    async def get_live_by_access_grant_id(self, access_grant_id: UUID) -> VPNCredential | None:
        # Не больше одной строки гарантирует частичный уникальный индекс uq_vpn_credentials_live_per_grant.
        sql_request = select(VPNCredential).where(
            VPNCredential.access_grant_id == access_grant_id,
            VPNCredential.revoked_at.is_(None),
        )
        result = await self.session.execute(sql_request)
        return result.scalar_one_or_none()

    async def list_with_access_by_protocol_id(
        self, protocol_id: UUID
    ) -> Sequence[tuple[VPNCredential, AccessGrant, Subscription]]:
        """Все credential'ы протокола (и живые, и отозванные) вместе с их грантом и подпиской — для sync."""
        sql_request = (
            select(VPNCredential, AccessGrant, Subscription)
            .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .where(AccessGrant.protocol_id == protocol_id)
        )
        result = await self.session.execute(sql_request)
        return result.tuples().all()

    async def has_live_for_subscription(self, subscription_id: UUID) -> bool:
        sql_request = (
            select(VPNCredential.id)
            .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
            .where(AccessGrant.subscription_id == subscription_id, VPNCredential.revoked_at.is_(None))
            .limit(1)
        )
        return (await self.session.execute(sql_request)).first() is not None

    async def has_live_for_server(self, server_id: UUID) -> bool:
        sql_request = (
            select(VPNCredential.id)
            .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
            .where(AccessGrant.server_id == server_id, VPNCredential.revoked_at.is_(None))
            .limit(1)
        )
        return (await self.session.execute(sql_request)).first() is not None

    async def has_live_for_user(self, user_id: UUID) -> bool:
        sql_request = (
            select(VPNCredential.id)
            .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .where(Subscription.user_id == user_id, VPNCredential.revoked_at.is_(None))
            .limit(1)
        )
        return (await self.session.execute(sql_request)).first() is not None
