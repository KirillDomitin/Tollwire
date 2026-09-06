#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AlreadyExistsError, NotFoundError
from modules.vpn_users.models.user import VPNUser
from modules.vpn_users.repository import VPNUserRepository
from modules.vpn_users.schemas import VPNUserCreate, VPNUserUpdate


class VPNUserService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.users = VPNUserRepository(session)

    async def create(self, data: VPNUserCreate) -> VPNUser:
        if await self.users.get_by_email(data.email):
            raise AlreadyExistsError
        new_user = VPNUser(username=data.username, email=data.email, description=data.description)
        user = await self.users.create(new_user)
        await self.session.commit()
        return user

    async def get(self, user_id: UUID) -> VPNUser:
        user = await self.users.get_by_id(user_id)
        if not user:
            raise NotFoundError
        return user

    async def get_by_email(self, email: str) -> VPNUser:
        user = await self.users.get_by_email(email)
        if not user:
            raise NotFoundError
        return user

    async def list_all(self) -> Sequence[VPNUser]:
        return await self.users.list_all()

    async def update(self, user_id: UUID, data: VPNUserUpdate) -> VPNUser:
        user = await self.get(user_id)
        fields = data.model_dump(exclude_unset=True)
        for field, value in fields.items():
            setattr(user, field, value)
        await self.session.commit()
        return user

    async def delete(self, user_id: UUID) -> None:
        user = await self.get(user_id)
        await self.users.delete(user)
        await self.session.commit()
