#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AlreadyExistsError, LiveCredentialExistsError, NotFoundError
from modules.access.repository import SubscriptionRepository, VPNCredentialRepository
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService, diff_changes
from modules.vpn_users.models.user import VPNUser
from modules.vpn_users.repository import VPNUserRepository
from modules.vpn_users.schemas import AccessSummary, VPNUserCreate, VPNUserListItem, VPNUserUpdate


class VPNUserService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.users = VPNUserRepository(session)
        self.audit = AuditService(session, actor)

    async def stage_create(
        self, data: VPNUserCreate, *, fields: dict[str, Any] | None = None, audit_meta: dict[str, Any] | None = None
    ) -> VPNUser:
        """Проверки, вставка (flush) и аудит без commit: вызывающий коммитит сам, вместе со своими шагами.

        fields - дополнительные колонки пользователя (telegram_id, status), audit_meta - вместо обычного meta события.
        """
        if await self.users.get_by_email(data.email):
            raise AlreadyExistsError
        new_user = VPNUser(username=data.username, email=data.email, description=data.description, **(fields or {}))
        user = await self.users.create(new_user)
        meta = audit_meta if audit_meta is not None else {"username": user.username, "email": user.email}
        self.audit.record(AuditAction.VPN_USER_CREATED, AuditEntity.VPN_USER, user.id, meta)
        return user

    async def create(self, data: VPNUserCreate) -> VPNUser:
        user = await self.stage_create(data)
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

    async def list_with_access(self) -> list[VPNUserListItem]:
        """Пользователи вместе с последней подпиской — два запроса на весь список, без запроса на каждого."""
        users = await self.users.list_all()
        latest = await SubscriptionRepository(self.session).latest_by_user()
        return [
            VPNUserListItem.model_validate(user).model_copy(
                update={"access": AccessSummary.model_validate(latest[user.id]) if user.id in latest else None}
            )
            for user in users
        ]

    async def update(self, user_id: UUID, data: VPNUserUpdate) -> VPNUser:
        user = await self.get(user_id)
        fields = data.model_dump(exclude_unset=True)
        if "email" in fields and fields["email"] != user.email and await self.users.get_by_email(fields["email"]):
            raise AlreadyExistsError
        changes = diff_changes(user, fields)
        for field, value in fields.items():
            setattr(user, field, value)
        if changes:
            self.audit.record(AuditAction.VPN_USER_UPDATED, AuditEntity.VPN_USER, user.id, {"changes": changes})
        await self.session.commit()
        return user

    async def delete(self, user_id: UUID) -> None:
        user = await self.get(user_id)
        # Каскад в БД удалил бы credential'ы, а peer'ы остались бы на сервере — поэтому сначала отзыв.
        if await VPNCredentialRepository(self.session).has_live_for_user(user_id):
            raise LiveCredentialExistsError("User has live VPN clients, revoke access first")
        await self.users.delete(user)
        self.audit.record(
            AuditAction.VPN_USER_DELETED,
            AuditEntity.VPN_USER,
            user.id,
            {"username": user.username, "email": user.email},
        )
        await self.session.commit()
