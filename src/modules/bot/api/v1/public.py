#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Публичная страница конфига: открывается по токену из ссылки, без входа в панель и без ключа бота.

Токен приходит в теле POST (из фрагмента ссылки `/c#токен`): ни в адрес, ни в логи доступа он не попадает. Любой отказ
отвечает одним и тем же телом и теми же заголовками: по ответу нельзя понять, существовал ли токен."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import NotFoundError
from core.rate_limit import RateLimiter
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_request_ip
from modules.bot.config_links import ConfigLinkService
from modules.bot.rate_limits import PUBLIC_CONFIG_FAILURES_BY_IP, PUBLIC_CONFIG_REQUESTS_BY_IP
from modules.bot.schemas import PublicConfigRead

router = APIRouter(prefix="/api/v1/public", tags=["public"])

# Страница с приватным ключом: ни кэша (браузер, прокси), ни индексации.
_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}


class PublicConfigRequest(BaseModel):
    # Длина не ограничена снизу намеренно: короткий мусор получает тот же 404, что и неверный токен.
    token: Annotated[str, StringConstraints(max_length=256)]


@router.post("/config", response_model=PublicConfigRead)
async def open_config(
    data: PublicConfigRequest, request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> JSONResponse:
    """Содержимое страницы конфига по токену. 404 без подробностей: токена нет, он заменён, доступ закончился."""
    limiter = RateLimiter()
    ip = get_request_ip(request) or "unknown"
    await limiter.ensure_not_exceeded(PUBLIC_CONFIG_FAILURES_BY_IP, ip)  # до обращения к БД: перебор не грузит её
    await limiter.hit(PUBLIC_CONFIG_REQUESTS_BY_IP, ip)
    try:
        page = await ConfigLinkService(session, AuditActor(admin_id=None, ip=ip)).open(data.token)
    except NotFoundError:
        await limiter.record(PUBLIC_CONFIG_FAILURES_BY_IP, ip)
        return JSONResponse(status_code=404, content={"detail": "Not found"}, headers=_HEADERS)
    return JSONResponse(content=page.model_dump(mode="json"), headers=_HEADERS)
