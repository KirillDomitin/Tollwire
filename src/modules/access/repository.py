#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.access.models import AccessGrantStatus
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription

_ACTIVE_STATUSES = (AccessGrantStatus.PENDING, AccessGrantStatus.ACTIVE)


class SubscriptionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, subscription: Subscription) -> Subscription:
        self.session.add(subscription)
        await self.session.flush()
        return subscription

    async def get_by_id(self, subscription_id: UUID) -> Subscription | None:
        return await self.session.get(Subscription, subscription_id)

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

    async def delete(self, subscription: Subscription) -> None:
        await self.session.delete(subscription)


class AccessGrantRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, access_grant: AccessGrant) -> AccessGrant:
        self.session.add(access_grant)
        await self.session.flush()
        return access_grant

    async def get_by_id(self, access_grant_id: UUID) -> AccessGrant | None:
        return await self.session.get(AccessGrant, access_grant_id)

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
