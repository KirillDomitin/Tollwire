#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Bot API: единственное, что видит Telegram-бот. Ключ в X-Bot-Key открывает только его (см. require_bot_key)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from core.exceptions import ReasonConflictError
from core.rate_limit import RateLimiter
from modules.bot.admin_links import AdminLinkService
from modules.bot.config_links import ConfigLinkService
from modules.bot.dependencies import BotKeyContext
from modules.bot.invites import BotInviteService
from modules.bot.keys import BotKeyService
from modules.bot.models.registration import BotRegistration
from modules.bot.outbox import OutboxService
from modules.bot.rate_limits import BOT_ADMIN_LINK_FAILURES_BY_KEY
from modules.bot.registrations import BotRegistrationService
from modules.bot.schemas import (
    AdminLinkRead,
    AdminLinkRequest,
    BotRegistrationRead,
    ConfigLinkRead,
    HeartbeatRequest,
    HeartbeatResponse,
    InviteCheckRequest,
    InviteCheckResponse,
    OutboxAckRequest,
    OutboxAckResponse,
    OutboxClaimRequest,
    OutboxClaimResponse,
    RegistrationCreate,
    UserStatusRead,
)

# Нет router-level зависимости: ключ - параметр каждого маршрута, ровно одна зависимость на маршрут (её проверяет тест).
router = APIRouter(prefix="/api/v1/bot", tags=["bot"])


def _read(registration: BotRegistration) -> BotRegistrationRead:
    return BotRegistrationRead(registration_id=registration.id, status=registration.status)


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(data: HeartbeatRequest, context: BotKeyContext, response: Response) -> HeartbeatResponse:
    """Бот сообщает имя и версию, получает чаты и порог наблюдения за панелью."""
    response.headers["Cache-Control"] = "no-store"
    return await BotKeyService(context.session, context.actor).heartbeat(context.key_id, data)


@router.post("/admin-links", response_model=AdminLinkRead)
async def admin_link(data: AdminLinkRequest, context: BotKeyContext) -> AdminLinkRead:
    limiter = RateLimiter()
    subject = str(context.key_id)
    await limiter.ensure_not_exceeded(BOT_ADMIN_LINK_FAILURES_BY_KEY, subject)
    try:
        result = await AdminLinkService(context.session, context.actor).link(
            data.code, data.telegram_id, data.telegram_username
        )
    except ReasonConflictError:
        await limiter.record(BOT_ADMIN_LINK_FAILURES_BY_KEY, subject)
        raise
    return AdminLinkRead(admin_name=result.admin_name)


@router.post("/invites/check", response_model=InviteCheckResponse)
async def check_invite(data: InviteCheckRequest, context: BotKeyContext) -> InviteCheckResponse:
    """valid / used / expired / revoked / unknown, без подробностей приглашения (протокол, срок, кому)."""
    status = await BotInviteService(context.session, context.actor).check(data.code)
    return InviteCheckResponse(status=status.value)


@router.post("/registrations", response_model=BotRegistrationRead, status_code=201)
async def register(data: RegistrationCreate, context: BotKeyContext) -> BotRegistrationRead:
    """Занять приглашение и начать выдачу доступа. Повтор тем же человеком возвращает ту же регистрацию;
    409 с `reason`: приглашение занято / истекло / недействительно, почта или Telegram заняты."""
    await context.limit_registrations()
    registration = await BotRegistrationService(context.session, context.actor).register(data, context.key_id)
    return _read(registration)


@router.get("/registrations/{registration_id}", response_model=BotRegistrationRead)
async def registration_status(registration_id: UUID, context: BotKeyContext) -> BotRegistrationRead:
    registration = await BotRegistrationService(context.session, context.actor).get(registration_id)
    return _read(registration)


@router.get("/users/by-telegram/{telegram_id}", response_model=UserStatusRead)
async def user_status(telegram_id: Annotated[int, Path(ge=1, le=2**63 - 1)], context: BotKeyContext) -> UserStatusRead:
    """/status в боте: имя, до какого числа доступ, протокол."""
    return await BotRegistrationService(context.session, context.actor).by_telegram(telegram_id)


@router.post("/users/by-telegram/{telegram_id}/config-link", response_model=ConfigLinkRead)
async def config_link(telegram_id: Annotated[int, Path(ge=1, le=2**63 - 1)], context: BotKeyContext) -> ConfigLinkRead:
    """Новая ссылка на страницу конфига; прежняя перестаёт открываться. Ссылка видна только в этом ответе.
    404: человека нет; 409 с `reason`: config_unavailable / not_ready / no_access / not_configured."""
    service = ConfigLinkService(context.session, context.actor)
    link, token = await service.issue_for_telegram(telegram_id)
    return await service.url_of(link, token)


@router.post("/outbox/claim", response_model=OutboxClaimResponse)
async def outbox_claim(data: OutboxClaimRequest, context: BotKeyContext) -> JSONResponse:
    """Бот забирает пачку исходящих сообщений в аренду. Для ссылок на конфиг адрес выпускается прямо сейчас и есть
    только в этом ответе (no-store); темп отправки держит бот, лимит Telegram - на нём."""
    kinds: list[str] | None = list(data.kinds) if data.kinds is not None else None
    result = await OutboxService(context.session, context.actor).claim(context.key_id, data.limit, kinds=kinds)
    items = []
    for item in result.items:
        if item.kind == "config":
            items.append(
                {
                    "id": item.id,
                    "kind": item.kind,
                    "telegram_id": item.telegram_id,
                    "config_url": item.config_url,
                    "valid_until": item.valid_until,
                }
            )
        elif item.kind == "notify":
            items.append(
                {
                    "id": item.id,
                    "kind": item.kind,
                    "telegram_id": item.telegram_id,
                    "text": item.text,
                    "format": item.format,
                }
            )
        else:
            items.append(
                {"id": item.id, "kind": item.kind, "telegram_id": item.telegram_id, "broadcast_id": item.broadcast_id}
            )
    body = {"lease_id": result.lease_id, "lease_until": result.lease_until, "items": items, "texts": result.texts}
    return JSONResponse(content=jsonable_encoder(body), headers={"Cache-Control": "no-store"})


@router.post("/outbox/ack", response_model=OutboxAckResponse)
async def outbox_ack(data: OutboxAckRequest, context: BotKeyContext) -> OutboxAckResponse:
    """Итоги отправки: sent / blocked / failed / retry. Принимаются только строки этой аренды, остальное - в ignored;
    повтор ничего не меняет."""
    result = await OutboxService(context.session, context.actor).ack(data.lease_id, data.results)
    return OutboxAckResponse(applied=result.applied, ignored=result.ignored)
