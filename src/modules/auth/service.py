#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import InvalidCredentialsError, InvalidDataError
from core.rate_limit import RateLimiter, subject_key
from core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from core.utils import get_refresh_expires_at
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.auth.models.admin import Admin
from modules.auth.rate_limits import LOGIN_FAILURES_BY_EMAIL, LOGIN_FAILURES_BY_EMAIL_IP, PASSWORD_CHANGE_FAILURES
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
        # Argon2 — ~100 мс CPU. В отдельном потоке: иначе на это время встаёт весь event loop (все запросы API).
        if admin is None:
            await asyncio.to_thread(verify_password, password, DUMMY_PASSWORD_HASH)  # то же время, что и неверный
            raise InvalidCredentialsError
        if not await asyncio.to_thread(verify_password, password, admin.password_hash) or not admin.is_active:
            raise InvalidCredentialsError
        return admin

    async def login(self, email: str, password: str) -> tuple[str, str]:
        email_key = subject_key(email)
        pair_key = f"{email_key}:{self.actor.ip or 'unknown'}"
        locks = ((LOGIN_FAILURES_BY_EMAIL_IP, pair_key, "ip"), (LOGIN_FAILURES_BY_EMAIL, email_key, "account"))
        # Закрыто после серии неудач: отказываем ДО проверки пароля. Иначе перебор продолжался бы, а верный
        # пароль выдал бы себя другим ответом. Работает одинаково и для несуществующих email.
        for limit, key, _ in locks:
            await self.limiter.ensure_not_exceeded(limit, key)
        try:
            admin = await self.authenticate(email, password)
        except InvalidCredentialsError:
            audit = AuditService(self.session, self.actor)
            # Только email: пароль (даже неверный) в журнал не пишем — это мог быть почти верный пароль.
            audit.record(AuditAction.LOGIN_FAILED, meta={"email": email})
            for limit, key, scope in locks:
                if await self.limiter.record(limit, key) == limit.max_hits:  # ровно на пороге — одна запись
                    audit.record(
                        AuditAction.LOGIN_LOCKED,
                        meta={"email": email, "scope": scope, "lock_seconds": limit.window_seconds},
                    )
            await self.session.commit()
            raise
        # Успех обнуляет счётчик своего адреса. Общий по учётке — нет: иначе каждый вход админа давал бы
        # перебору с ботнета новую порцию попыток.
        await self.limiter.reset(LOGIN_FAILURES_BY_EMAIL_IP, pair_key)
        admin.last_login_at = datetime.now(timezone.utc)
        access = create_access_token(str(admin.id))
        refresh = generate_refresh_token()
        refresh_hash = hash_refresh_token(refresh)
        expires_at = get_refresh_expires_at()
        await self.sessions.create(admin_id=admin.id, token_hash=refresh_hash, expires_at=expires_at)
        AuditService(self.session, self.actor.as_admin(admin.id)).record(AuditAction.LOGIN, AuditEntity.ADMIN, admin.id)
        await self.session.commit()
        return access, refresh

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        # Старый токен изымается атомарно (одноразовый) — дальше решаем, выдавать ли новый.
        taken = await self.sessions.take(hash_refresh_token(refresh_token))
        if taken is None:
            raise InvalidCredentialsError
        admin_id, expires_at, revoked = taken
        admin = await self.admins.get_by_id(admin_id)
        # Отключённый админ не продлевает сессию, даже если refresh-токен ещё не истёк.
        if revoked or expires_at < datetime.now(timezone.utc) or admin is None or not admin.is_active:
            await self.session.commit()  # изъятие негодного токена тоже фиксируем
            raise InvalidCredentialsError
        new_refresh = generate_refresh_token()
        await self.sessions.create(admin_id, hash_refresh_token(new_refresh), get_refresh_expires_at())
        await self.session.commit()
        return create_access_token(str(admin_id)), new_refresh

    async def change_password(self, admin: Admin, current_password: str, new_password: str) -> tuple[str, str]:
        """Смена пароля самим админом. Завершает все прежние сессии и выдаёт новую пару токенов этой вкладке.

        Неверный текущий пароль → InvalidCredentialsError (и засчитывается: см. PASSWORD_CHANGE_FAILURES).
        """
        key = str(admin.id)
        await self.limiter.ensure_not_exceeded(PASSWORD_CHANGE_FAILURES, key)
        # Строка админа блокируется на время смены: два одновременных запроса с одним текущим паролем не пройдут оба.
        await self.session.refresh(admin, with_for_update=True)
        audit = AuditService(self.session, self.actor.as_admin(admin.id))
        if not await asyncio.to_thread(verify_password, current_password, admin.password_hash):
            audit.record(AuditAction.PASSWORD_CHANGE_FAILED, AuditEntity.ADMIN, admin.id)
            await self.session.commit()
            await self.limiter.record(PASSWORD_CHANGE_FAILURES, key)
            raise InvalidCredentialsError
        if new_password == current_password:
            await self.session.rollback()
            raise InvalidDataError("New password must differ from the current one")
        admin.password_hash = await asyncio.to_thread(hash_password, new_password)
        admin.must_change_password = False
        admin.password_changed_at = datetime.now(timezone.utc)
        revoked = await self.sessions.delete_for_admin(admin.id)
        refresh = generate_refresh_token()
        await self.sessions.create(admin.id, hash_refresh_token(refresh), get_refresh_expires_at())
        # Токен выдаётся уже после password_changed_at: его iat (целые секунды) не меньше отметки и проверку проходит.
        access = create_access_token(str(admin.id))
        audit.record(AuditAction.PASSWORD_CHANGED, AuditEntity.ADMIN, admin.id, {"sessions_revoked": revoked})
        await self.session.commit()
        await self.limiter.reset(PASSWORD_CHANGE_FAILURES, key)
        return access, refresh

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
