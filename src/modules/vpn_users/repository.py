#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.vpn_users.models.user import VPNUser


class VPNUserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, user_id: UUID) -> VPNUser | None:
        return await self.session.get(VPNUser, user_id)

    async def get_by_email(self, email: str) -> VPNUser | None:
        sql_request = select(VPNUser).where(VPNUser.email == email)
        user = await self.session.execute(sql_request)
        return user.scalar_one_or_none()

    async def get_by_email_ignore_case(self, email: str) -> VPNUser | None:
        """Почту сравнивает без учёта регистра: уникальный индекс БД регистр различает, а люди пишут по-разному."""
        user = await self.session.execute(select(VPNUser).where(func.lower(VPNUser.email) == email.lower()))
        return user.scalars().first()

    async def get_by_telegram_id(self, telegram_id: int) -> VPNUser | None:
        user = await self.session.execute(select(VPNUser).where(VPNUser.telegram_id == telegram_id))
        return user.scalar_one_or_none()

    async def create(self, user: VPNUser) -> VPNUser:
        self.session.add(user)
        await self.session.flush()
        return user

    async def list_all(self) -> Sequence[VPNUser]:
        sql_request = select(VPNUser)
        user_list = await self.session.execute(sql_request)
        return user_list.scalars().all()

    async def delete(self, user: VPNUser) -> None:
        await self.session.delete(user)
