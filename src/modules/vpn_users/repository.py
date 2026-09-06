#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
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
