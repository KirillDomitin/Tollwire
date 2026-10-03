#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.auth.models.admin import Admin
from modules.auth.models.admin_session import AdminSession


class AdminRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_email(self, email: str) -> Admin | None:
        request = select(Admin).where(Admin.email == email)
        result = await self.session.execute(request)
        return result.scalar_one_or_none()

    async def get_by_id(self, admin_id: UUID) -> Admin | None:
        return await self.session.get(Admin, admin_id)


class AdminSessionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, admin_id: UUID, token_hash: str, expires_at: datetime) -> AdminSession:
        admin_session = AdminSession(admin_id=admin_id, token_hash=token_hash, expires_at=expires_at)
        self.session.add(admin_session)
        await self.session.flush()
        return admin_session

    async def get_by_token_hash(self, token_hash: str) -> AdminSession | None:
        request = select(AdminSession).where(AdminSession.token_hash == token_hash)
        result = await self.session.execute(request)
        return result.scalar_one_or_none()

    async def delete(self, session_obj: AdminSession) -> None:
        await self.session.delete(session_obj)

    async def delete_expired(self, now: datetime) -> int:
        """Удаляет сессии с истёкшим refresh-токеном одним запросом. Возвращает число удалённых."""
        # DELETE возвращает CursorResult (у него есть rowcount), а execute() типизирован общим Result.
        result = cast(
            CursorResult[Any], await self.session.execute(delete(AdminSession).where(AdminSession.expires_at < now))
        )
        return int(result.rowcount)
