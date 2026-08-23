#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import InvalidCredentialsError
from core.security import create_access_token, generate_refresh_token, hash_refresh_token, verify_password
from core.utils import get_refresh_expires_at
from modules.auth.models.admin import Admin
from modules.auth.repository import AdminRepository, AdminSessionRepository


class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.admins = AdminRepository(session)
        self.sessions = AdminSessionRepository(session)

    async def authenticate(self, email: str, password: str) -> Admin:
        admin = await self.admins.get_by_email(email)
        if not admin or not verify_password(password, admin.password_hash) or not admin.is_active:
            raise InvalidCredentialsError
        return admin

    async def login(self, email: str, password: str) -> tuple[str, str]:
        admin = await self.authenticate(email, password)
        access = create_access_token(str(admin.id))
        refresh = generate_refresh_token()
        refresh_hash = hash_refresh_token(refresh)
        expires_at = get_refresh_expires_at()
        await self.sessions.create(admin_id=admin.id, token_hash=refresh_hash, expires_at=expires_at)
        await self.session.commit()
        return access, refresh

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        refresh_hash = hash_refresh_token(refresh_token)
        admin_session = await self.sessions.get_by_token_hash(refresh_hash)
        if not admin_session or admin_session.revoked or admin_session.expires_at < datetime.now(timezone.utc):
            raise InvalidCredentialsError
        new_access = create_access_token(str(admin_session.admin_id))
        new_refresh = generate_refresh_token()
        new_refresh_hash = hash_refresh_token(new_refresh)
        new_expires_at = get_refresh_expires_at()
        await self.sessions.create(admin_session.admin_id, new_refresh_hash, new_expires_at)
        await self.sessions.delete(admin_session)
        await self.session.commit()
        return new_access, new_refresh

    async def logout(self, refresh_token: str) -> None:
        refresh_hash = hash_refresh_token(refresh_token)
        admin_session = await self.sessions.get_by_token_hash(refresh_hash)
        if admin_session:
            await self.sessions.delete(admin_session)
            await self.session.commit()
