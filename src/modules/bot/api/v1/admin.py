#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Админский API бота: состояние, ключи, приглашения, регистрации, настройки, рассылки, ссылка одному человеку.
Права: чтение - bot:read, записи (приглашения, рассылки, настройки, повтор регистрации) - bot:manage, ключи - bot:keys
(только superadmin)."""

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import NotFoundError
from core.rate_limit import RateLimiter
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.dependencies import get_current_admin, require
from modules.auth.models.admin import Admin
from modules.auth.permissions import Permission
from modules.bot.invites import BotInviteService, invite_status
from modules.bot.keys import BotKeyService
from modules.bot.models.invite import BotInvite
from modules.bot.models.registration import BotRegistration, RegistrationStatus
from modules.bot.outbox import OutboxService
from modules.bot.page import BotPageService, broadcast_read
from modules.bot.rate_limits import BOT_BROADCASTS_BY_ADMIN, BOT_CONFIG_SENDS_BY_ADMIN
from modules.bot.registrations import BotRegistrationService
from modules.bot.repository import OutboxRepository
from modules.bot.schemas import (
    AudienceRead,
    AudienceRequest,
    BotKeyCreate,
    BotKeyCreated,
    BotKeyRead,
    BotProtocolRead,
    BotSettingsRead,
    BotSettingsUpdate,
    BotStatusRead,
    BroadcastCreate,
    BroadcastRead,
    ConfigLinkQueued,
    InviteCreate,
    InviteCreated,
    InviteRead,
    OutboxRowRead,
    RecipientRead,
    RegistrationRead,
)
from modules.bot.settings import BotSettingsService

router = APIRouter(prefix="/api/v1/bot-admin", tags=["bot-admin"], dependencies=[Depends(get_current_admin)])

Session = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[AuditActor, Depends(get_audit_actor)]


Cursor = tuple[datetime, UUID] | None


def cursor(before_created_at: datetime | None, before_id: UUID | None) -> Cursor:
    """Курсор списка: строки строго старше (created_at, id) последней показанной; обе половины вместе."""
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="before_created_at and before_id go together")
    return (before_created_at, before_id) if before_created_at is not None and before_id is not None else None


def invite_read(invite: BotInvite, used_by_user_id: UUID | None = None, used_by_name: str | None = None) -> InviteRead:
    return InviteRead(
        used_by_user_id=used_by_user_id,
        used_by_name=used_by_name,
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
async def list_invites(
    session: Session,
    before_created_at: datetime | None = None,
    before_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[InviteRead]:
    """Приглашения, новые первыми; у занятых - кто занял. Код приглашения в списке не показывается."""
    rows = await BotPageService(session).invites(limit, cursor(before_created_at, before_id))
    return [invite_read(invite, user_id, name) for invite, user_id, name in rows]


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
    session: Session,
    status: RegistrationStatus | None = None,
    before_created_at: datetime | None = None,
    before_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> Sequence[BotRegistration]:
    return await BotPageService(session).registrations(status, limit, cursor(before_created_at, before_id))


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


# --- состояние, настройки, справочники -----------------------------------------------------------------------------


@router.get("/status", dependencies=[Depends(require(Permission.BOT_READ))], response_model=BotStatusRead)
async def bot_status(session: Session) -> BotStatusRead:
    """Бот: имя, версия, когда выходил на связь и забирал очередь; число сообщений в очереди."""
    return await BotPageService(session).status()


@router.get("/settings", dependencies=[Depends(require(Permission.BOT_READ))], response_model=BotSettingsRead)
async def get_settings(session: Session, actor: Actor) -> BotSettingsRead:
    return BotSettingsRead.model_validate(await BotSettingsService(session, actor).get())


@router.put("/settings", dependencies=[Depends(require(Permission.BOT_MANAGE))], response_model=BotSettingsRead)
async def update_settings(data: BotSettingsUpdate, session: Session, actor: Actor) -> BotSettingsRead:
    return BotSettingsRead.model_validate(await BotSettingsService(session, actor).update(data))


@router.get("/protocols", dependencies=[Depends(require(Permission.BOT_READ))], response_model=list[BotProtocolRead])
async def list_protocols(session: Session) -> list[BotProtocolRead]:
    """Протоколы, на которые можно пригласить и которым можно сделать рассылку."""
    return await BotPageService(session).protocols()


@router.get("/recipients", dependencies=[Depends(require(Permission.BOT_READ))], response_model=list[RecipientRead])
async def list_recipients(
    session: Session,
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RecipientRead]:
    """Люди с привязанным Telegram для выбора получателей: поиск по имени и по имени в Telegram."""
    return await BotPageService(session).recipients(search, limit)


# --- рассылки ------------------------------------------------------------------------------------------------------


@router.get("/broadcasts", dependencies=[Depends(require(Permission.BOT_READ))], response_model=list[BroadcastRead])
async def list_broadcasts(
    session: Session,
    before_created_at: datetime | None = None,
    before_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[BroadcastRead]:
    return await BotPageService(session).broadcasts(limit, cursor(before_created_at, before_id))


@router.post(
    "/broadcasts/audience", dependencies=[Depends(require(Permission.BOT_MANAGE))], response_model=AudienceRead
)
async def broadcast_audience(data: AudienceRequest, session: Session, actor: Actor) -> AudienceRead:
    """Сколько людей получит рассылка с таким отбором, не создавая её."""
    audience = await OutboxService(session, actor).audience(data)
    return AudienceRead(
        recipients=audience.recipients, without_telegram=audience.without_telegram, blocked=audience.blocked
    )


@router.post(
    "/broadcasts", dependencies=[Depends(require(Permission.BOT_MANAGE))], response_model=BroadcastRead, status_code=201
)
async def create_broadcast(
    data: BroadcastCreate, session: Session, actor: Actor, admin: Annotated[Admin, Depends(get_current_admin)]
) -> BroadcastRead:
    """Поставить рассылку в очередь бота. Повтор с тем же request_id возвращает ту же рассылку.
    422 с кодом telegram_html_*: текст не годится для Telegram; 409 not_configured: адрес панели не задан."""
    await RateLimiter().hit(BOT_BROADCASTS_BY_ADMIN, str(admin.id))
    return broadcast_read(await OutboxService(session, actor).create_broadcast(data))


@router.get(
    "/broadcasts/{broadcast_id}", dependencies=[Depends(require(Permission.BOT_READ))], response_model=BroadcastRead
)
async def get_broadcast(broadcast_id: UUID, session: Session) -> BroadcastRead:
    broadcast = await OutboxRepository(session).get_broadcast(broadcast_id)
    if broadcast is None:
        raise NotFoundError(f"Broadcast '{broadcast_id}' not found")
    return broadcast_read(broadcast)


@router.post(
    "/broadcasts/{broadcast_id}/cancel",
    dependencies=[Depends(require(Permission.BOT_MANAGE))],
    response_model=BroadcastRead,
)
async def cancel_broadcast(broadcast_id: UUID, session: Session, actor: Actor) -> BroadcastRead:
    """Ждущие сообщения закрываются, уже выданные боту доходят. Повтор ничего не меняет."""
    return broadcast_read(await OutboxService(session, actor).cancel_broadcast(broadcast_id))


# --- ссылка на конфиг одному человеку ------------------------------------------------------------------------------


@router.post(
    "/users/{user_id}/config-link",
    dependencies=[Depends(require(Permission.BOT_MANAGE))],
    response_model=ConfigLinkQueued,
    status_code=202,
)
async def send_config_link(
    user_id: UUID, session: Session, actor: Actor, admin: Annotated[Admin, Depends(get_current_admin)]
) -> ConfigLinkQueued:
    """«Отправить ссылку в Telegram»: новая ссылка выпускается, когда бот заберёт сообщение. Если открытая отправка
    уже есть - возвращается она. 409 с reason: no_telegram / bot_blocked / not_configured / no_access /
    config_unavailable."""
    await RateLimiter().hit(BOT_CONFIG_SENDS_BY_ADMIN, str(admin.id))
    row = await OutboxService(session, actor).queue_config_link(user_id)
    return ConfigLinkQueued(outbox_id=row.id, status=row.status)


@router.get("/outbox/{outbox_id}", dependencies=[Depends(require(Permission.BOT_READ))], response_model=OutboxRowRead)
async def get_outbox_row(outbox_id: UUID, session: Session) -> OutboxRowRead:
    """Состояние одного сообщения очереди: pending / claimed / sent / blocked / failed / skipped / cancelled."""
    row = await OutboxRepository(session).get_row(outbox_id)
    if row is None:
        raise NotFoundError(f"Message '{outbox_id}' not found")
    return OutboxRowRead(id=row.id, status=row.status, error=row.error)
