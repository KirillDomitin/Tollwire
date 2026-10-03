#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import InvalidCredentialsError
from core.rate_limit import RateLimiter, subject_key
from core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from core.utils import get_refresh_expires_at
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.auth.models.admin import Admin
from modules.auth.rate_limits import LOGIN_FAILURES_BY_EMAIL
from modules.auth.repository import AdminRepository, AdminSessionRepository


class AuthService:
    def __init__(self, session: AsyncSession, actor: AuditActor, limiter: RateLimiter | None = None):
        self.session = session
        self.admins = AdminRepository(session)
        self.sessions = AdminSessionRepository(session)
        self.actor = actor
        self.limiter = limiter or RateLimiter()

    async def authenticate(self, email: str, password: str) -> Admin:
        admin = await self.admins.get_by_email(email)
        if admin is None:
            verify_password(password, DUMMY_PASSWORD_HASH)  # то же время ответа, что и при неверном пароле
            raise InvalidCredentialsError
        if not verify_password(password, admin.password_hash) or not admin.is_active:
            raise InvalidCredentialsError
        return admin

    async def login(self, email: str, password: str) -> tuple[str, str]:
        email_key = subject_key(email)
        # Учётка закрыта после серии неудач: отказываем ДО проверки пароля. Иначе перебор продолжался бы,
        # а верный пароль выдал бы себя другим ответом. Работает одинаково и для несуществующих email.
        await self.limiter.ensure_not_exceeded(LOGIN_FAILURES_BY_EMAIL, email_key)
        try:
            admin = await self.authenticate(email, password)
        except InvalidCredentialsError:
            failures = await self.limiter.record(LOGIN_FAILURES_BY_EMAIL, email_key)
            audit = AuditService(self.session, self.actor)
            # Только email: пароль (даже неверный) в журнал не пишем — это мог быть почти верный пароль.
            audit.record(AuditAction.LOGIN_FAILED, meta={"email": email})
            if failures == LOGIN_FAILURES_BY_EMAIL.max_hits:  # ровно на пороге — одна запись, а не на каждую попытку
                audit.record(
                    AuditAction.LOGIN_LOCKED,
                    meta={"email": email, "lock_seconds": LOGIN_FAILURES_BY_EMAIL.window_seconds},
                )
            await self.session.commit()
            raise
        await self.limiter.reset(LOGIN_FAILURES_BY_EMAIL, email_key)  # успешный вход обнуляет счётчик неудач
        access = create_access_token(str(admin.id))
        refresh = generate_refresh_token()
        refresh_hash = hash_refresh_token(refresh)
        expires_at = get_refresh_expires_at()
        await self.sessions.create(admin_id=admin.id, token_hash=refresh_hash, expires_at=expires_at)
        AuditService(self.session, self.actor.as_admin(admin.id)).record(AuditAction.LOGIN, AuditEntity.ADMIN, admin.id)
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

    async def cleanup_expired_sessions(self) -> int:
        """Удаляет сессии с истёкшим refresh-токеном (вызывает планировщик). Войти по ним и так нельзя."""
        deleted = await self.sessions.delete_expired(datetime.now(timezone.utc))
        await self.session.commit()
        return deleted

    async def logout(self, refresh_token: str) -> None:
        refresh_hash = hash_refresh_token(refresh_token)
        admin_session = await self.sessions.get_by_token_hash(refresh_hash)
        if admin_session:
            await self.sessions.delete(admin_session)
            audit = AuditService(self.session, self.actor.as_admin(admin_session.admin_id))
            audit.record(AuditAction.LOGOUT, AuditEntity.ADMIN, admin_session.admin_id)
            await self.session.commit()
