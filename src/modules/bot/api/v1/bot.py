#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Bot API: единственное, что видит Telegram-бот. Ключ в X-Bot-Key открывает только его (см. require_bot_key)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path

from modules.bot.config_links import ConfigLinkService
from modules.bot.dependencies import BotKeyContext
from modules.bot.invites import BotInviteService
from modules.bot.keys import BotKeyService
from modules.bot.models.registration import BotRegistration
from modules.bot.registrations import BotRegistrationService
from modules.bot.schemas import (
    BotRegistrationRead,
    ConfigLinkRead,
    HeartbeatRequest,
    InviteCheckRequest,
    InviteCheckResponse,
    RegistrationCreate,
    UserStatusRead,
)

# Нет router-level зависимости: ключ - параметр каждого маршрута, ровно одна зависимость на маршрут (её проверяет тест).
router = APIRouter(prefix="/api/v1/bot", tags=["bot"])


def _read(registration: BotRegistration) -> BotRegistrationRead:
    return BotRegistrationRead(registration_id=registration.id, status=registration.status)


@router.post("/heartbeat")
async def heartbeat(data: HeartbeatRequest, context: BotKeyContext) -> dict[str, object]:
    """Бот сообщает, что жив: имя и версия. Ответ пока пустой, поля появятся вместе с настройками бота."""
    await BotKeyService(context.session, context.actor).heartbeat(context.key_id, data)
    return {}


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
