#!/usr/bin/python
# -*- coding: utf-8 -*-

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.postgres import engine, get_session
from core.exceptions import (
    AdminRuleError,
    AlreadyExistsError,
    AppError,
    BotAuthError,
    ConfigUnavailableError,
    CredentialChangedError,
    CredentialNotFound,
    GatewayExitError,
    ImportNotAllowedError,
    InactiveAccessError,
    InvalidDataError,
    JobAlreadyRunningError,
    LiveCredentialExistsError,
    NotFoundError,
    ReasonConflictError,
    RegistrationConflictError,
    SSHHostKeyNotPinnedError,
    StateConflictError,
)
from core.log import setup_logging
from core.middleware import RequestContextMiddleware
from core.rate_limit import RateLimitExceeded
from core.redis import get_redis
from modules.access.api.v1.router import router as access_router
from modules.audit.api.v1.router import router as audit_router
from modules.auth.api.v1.admins import router as admins_router
from modules.auth.api.v1.router import router as auth_router
from modules.bot.api.v1.admin import router as bot_admin_router
from modules.bot.api.v1.bot import router as bot_router
from modules.gateway.api.v1.router import router as gateway_router
from modules.geoip.api.v1.router import router as geoip_router
from modules.jobs.api.v1.router import router as job_router
from modules.monitoring.api.v1.router import router as monitoring_router
from modules.servers.api.v1.router import router as server_router
from modules.vpn_users.api.v1.router import router as vpn_user_router

setup_logging(settings.log_level, settings.log_format)
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    logger.info("api started")
    yield
    # Аккуратно закрыть пулы: незакрытые соединения PostgreSQL/Redis при остановке оставляют висящие сессии.
    await engine.dispose()
    await get_redis().aclose()
    logger.info("api stopped")


app = FastAPI(
    title=settings.project_name,
    docs_url="/api/openapi" if settings.docs_enabled else None,
    openapi_url="/api/openapi.json" if settings.docs_enabled else None,
    lifespan=lifespan,
)

app.add_middleware(RequestContextMiddleware)


# Страховочная сетка: доменная ошибка, которую роутер не перевёл в HTTP сам, — осмысленный 4xx, а не 500.
# Порядок важен: Starlette ищет обработчик по MRO исключения, поэтому подклассы AppError — отдельными записями.
_STATUS_BY_ERROR: dict[type[AppError], int] = {
    NotFoundError: 404,
    BotAuthError: 401,  # ключ бота не передан / неверный / отозван
    ReasonConflictError: 409,  # действие невозможно, причина в теле (reason)
    RegistrationConflictError: 409,  # приглашение занято, почта или Telegram заняты (reason в теле)
    InvalidDataError: 422,
    AlreadyExistsError: 409,
    CredentialNotFound: 409,  # действие невозможно, пока не задана SSH-учётка
    LiveCredentialExistsError: 409,
    InactiveAccessError: 409,
    AdminRuleError: 409,
    SSHHostKeyNotPinnedError: 409,  # сначала scan + pin ключа сервера
    JobAlreadyRunningError: 409,  # такая же задача по серверу ещё не завершена
    CredentialChangedError: 409,
    ConfigUnavailableError: 409,  # импортированный клиент: конфига нет
    ImportNotAllowedError: 409,  # ключа нет в отчёте поиска или peer не поддерживается
    GatewayExitError: 409,  # шлюз не установлен / выход не подходит / уже добавлен
    StateConflictError: 409,  # действие невозможно в текущем состоянии объекта
}


async def domain_error(request: Request, exc: Exception) -> JSONResponse:
    status = next((code for cls, code in _STATUS_BY_ERROR.items() if isinstance(exc, cls)), 409)
    content = {"detail": str(exc) or type(exc).__name__}
    if isinstance(exc, ReasonConflictError):
        content["reason"] = exc.reason
    return JSONResponse(status_code=status, content=content)


async def integrity_error(request: Request, exc: Exception) -> JSONResponse:
    # Уникальный индекс поймал то, что проверка в сервисе пропустила (две вставки одновременно). Текст
    # ошибки БД наружу не отдаём: в нём имена таблиц и значения.
    logger.warning("integrity error", extra={"error": type(exc).__name__})
    return JSONResponse(status_code=409, content={"detail": "Conflict with existing data"})


for error_class in _STATUS_BY_ERROR:
    app.add_exception_handler(error_class, domain_error)
app.add_exception_handler(IntegrityError, integrity_error)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Как стандартный ответ FastAPI, но без поля input: в нём присланное значение целиком, а в теле бывают
    # пароль и приватный ключ (например, пароль без логина в POST /ssh-key вернулся бы в 422 открытым текстом).
    errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.exception_handler(RateLimitExceeded)
async def rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    # Retry-After — через сколько секунд можно повторить. Какое правило сработало, наружу не сообщаем.
    return JSONResponse(
        status_code=429, content={"detail": "Too many requests"}, headers={"Retry-After": str(exc.retry_after)}
    )


app.include_router(auth_router)
app.include_router(admins_router)
app.include_router(server_router)
app.include_router(gateway_router)
app.include_router(geoip_router)
app.include_router(job_router)
app.include_router(vpn_user_router)
app.include_router(access_router)
app.include_router(audit_router)
app.include_router(monitoring_router)
app.include_router(bot_router)
app.include_router(bot_admin_router)


@app.get("/health")
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
