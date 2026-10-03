#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AdminRuleError, AlreadyExistsError, NotFoundError
from core.security import hash_password
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService, diff_changes
from modules.auth.models.admin import Admin, AdminRole
from modules.auth.repository import AdminRepository, AdminSessionRepository
from modules.auth.schemas import AdminCreate, AdminUpdate


class AdminService:
    """Управление админами панели (только superadmin). Удаления нет — только отключение: история аудита
    ссылается на админа, и «кто что делал» должно оставаться читаемым."""

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.admins = AdminRepository(session)
        self.sessions = AdminSessionRepository(session)
        self.audit = AuditService(session, actor)
        self.actor = actor

    async def list_all(self) -> Sequence[Admin]:
        return await self.admins.list_all()

    async def get(self, admin_id: UUID) -> Admin:
        admin = await self.admins.get_by_id(admin_id)
        if not admin:
            raise NotFoundError
        return admin

    async def create(self, data: AdminCreate) -> Admin:
        email = str(data.email)
        if await self.admins.get_by_email(email):
            raise AlreadyExistsError
        password_hash = await asyncio.to_thread(hash_password, data.password)  # Argon2 — не в event loop
        admin = Admin(email=email, password_hash=password_hash, role=data.role)
        self.admins.add(admin)
        await self.session.flush()
        self.audit.record(AuditAction.ADMIN_CREATED, AuditEntity.ADMIN, admin.id, {"email": email, "role": data.role})
        await self.session.commit()
        return admin

    async def update(self, admin_id: UUID, data: AdminUpdate) -> Admin:
        admin = await self.get(admin_id)
        changes = diff_changes(admin, data.model_dump(exclude_unset=True, exclude_none=True))
        if not changes:
            return admin
        if admin.id == self.actor.admin_id:
            # Иначе можно случайно запереть себя (или единственного управляющего) снаружи.
            raise AdminRuleError("You cannot change your own role or deactivate yourself")
        loses_superadmin = (
            admin.role == AdminRole.SUPERADMIN
            and admin.is_active
            and (
                changes.get("role", [None, AdminRole.SUPERADMIN])[1] != AdminRole.SUPERADMIN
                or changes.get("is_active", [None, True])[1] is False
            )
        )
        if loses_superadmin and len(await self.admins.lock_active_superadmin_ids()) <= 1:
            raise AdminRuleError("Cannot demote or deactivate the last active superadmin")

        for field, (_, value) in changes.items():
            setattr(admin, field, value)
        # Отключили или сменили роль — завершаем сессии: refresh-токены больше не работают, админ входит заново.
        # (Access-токен и так перестаёт пускать сразу: роль и is_active читаются из БД на каждом запросе.)
        revoked = await self.sessions.delete_for_admin(admin.id)
        self.audit.record(
            AuditAction.ADMIN_UPDATED,
            AuditEntity.ADMIN,
            admin.id,
            {"email": admin.email, "changes": changes, "sessions_revoked": revoked},
        )
        await self.session.commit()
        return admin
