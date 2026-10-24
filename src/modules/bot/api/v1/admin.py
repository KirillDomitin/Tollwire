#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Админский API бота: ключи, приглашения, регистрации. Права: чтение - bot:read, приглашения и повтор регистрации -
bot:manage, ключи - bot:keys (только superadmin)."""

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.dependencies import get_current_admin, require
from modules.auth.permissions import Permission
from modules.bot.invites import BotInviteService, invite_status
from modules.bot.keys import BotKeyService
from modules.bot.models.invite import BotInvite
from modules.bot.models.registration import BotRegistration, RegistrationStatus
from modules.bot.registrations import BotRegistrationService
from modules.bot.schemas import (
    BotKeyCreate,
    BotKeyCreated,
    BotKeyRead,
    InviteCreate,
    InviteCreated,
    InviteRead,
    RegistrationRead,
)

router = APIRouter(prefix="/api/v1/bot-admin", tags=["bot-admin"], dependencies=[Depends(get_current_admin)])

Session = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[AuditActor, Depends(get_audit_actor)]


def invite_read(invite: BotInvite) -> InviteRead:
    return InviteRead(
        id=invite.id,
        protocol_id=invite.protocol_id,
        days=invite.days,
        expires_at=invite.expires_at,
        note=invite.note,
        created_at=invite.created_at,
        used_at=invite.used_at,
        revoked_at=invite.revoked_at,
        status=invite_status(invite, datetime.now(timezone.utc)).value,
    )


# --- ключи ---------------------------------------------------------------------------------------------------------


@router.get("/keys", dependencies=[Depends(require(Permission.BOT_READ))], response_model=list[BotKeyRead])
async def list_keys(session: Session, actor: Actor) -> Sequence[BotKeyRead]:
    return [BotKeyRead.model_validate(key) for key in await BotKeyService(session, actor).list()]


@router.post(
    "/keys", dependencies=[Depends(require(Permission.BOT_KEYS))], response_model=BotKeyCreated, status_code=201
)
async def create_key(data: BotKeyCreate, session: Session, actor: Actor) -> BotKeyCreated:
    """Выпустить ключ бота. Открытое значение (`key`) показывается только в этом ответе."""
    key, secret = await BotKeyService(session, actor).create(data)
    return BotKeyCreated(**BotKeyRead.model_validate(key).model_dump(), key=secret)


@router.post("/keys/{key_id}/revoke", dependencies=[Depends(require(Permission.BOT_KEYS))], response_model=BotKeyRead)
async def revoke_key(key_id: UUID, session: Session, actor: Actor) -> BotKeyRead:
    """Отозвать ключ: бот с ним перестаёт работать немедленно."""
    return BotKeyRead.model_validate(await BotKeyService(session, actor).revoke(key_id))


# --- приглашения ---------------------------------------------------------------------------------------------------


@router.get("/invites", dependencies=[Depends(require(Permission.BOT_READ))], response_model=list[InviteRead])
async def list_invites(session: Session, actor: Actor) -> list[InviteRead]:
    return [invite_read(invite) for invite in await BotInviteService(session, actor).list()]


@router.post(
    "/invites", dependencies=[Depends(require(Permission.BOT_MANAGE))], response_model=InviteCreated, status_code=201
)
async def create_invite(data: InviteCreate, session: Session, actor: Actor) -> InviteCreated:
    """Создать одноразовое приглашение. Код и ссылка показываются только в этом ответе."""
    service = BotInviteService(session, actor)
    invite, code = await service.create(data)
    return InviteCreated(**invite_read(invite).model_dump(), code=code, link=await service.link(code))


@router.post(
    "/invites/{invite_id}/revoke", dependencies=[Depends(require(Permission.BOT_MANAGE))], response_model=InviteRead
)
async def revoke_invite(invite_id: UUID, session: Session, actor: Actor) -> InviteRead:
    return invite_read(await BotInviteService(session, actor).revoke(invite_id))


# --- регистрации ---------------------------------------------------------------------------------------------------


@router.get(
    "/registrations", dependencies=[Depends(require(Permission.BOT_READ))], response_model=list[RegistrationRead]
)
async def list_registrations(
    session: Session, actor: Actor, status: RegistrationStatus | None = None
) -> Sequence[BotRegistration]:
    return await BotRegistrationService(session, actor).list(status)


@router.get(
    "/registrations/{registration_id}",
    dependencies=[Depends(require(Permission.BOT_READ))],
    response_model=RegistrationRead,
)
async def get_registration(registration_id: UUID, session: Session, actor: Actor) -> BotRegistration:
    return await BotRegistrationService(session, actor).get(registration_id)


@router.post(
    "/registrations/{registration_id}/retry",
    dependencies=[Depends(require(Permission.BOT_MANAGE))],
    response_model=RegistrationRead,
    status_code=202,
)
async def retry_registration(registration_id: UUID, session: Session, actor: Actor) -> BotRegistration:
    """Повторить выдачу упавшей регистрации (только из `failed`): тот же пользователь и доступ, новая задача."""
    return await BotRegistrationService(session, actor).retry(registration_id)
