#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Awaitable, Callable

from fastapi import Request

from core.config import settings
from core.rate_limit import Limit, RateLimiter
from modules.audit.dependencies import get_request_ip

# Все попытки входа с одного IP — отсекает перебор с одного адреса ещё до Argon2.
LOGIN_BY_IP = Limit("login_ip", settings.login_ip_max_attempts, settings.login_ip_window_seconds)
# Неудачные входы в одну учётку с любых IP — отсекает перебор с ботнета. Считаются только провалы.
LOGIN_FAILURES_BY_EMAIL = Limit("login_email_failures", settings.login_max_failures, settings.login_lock_seconds)
REFRESH_BY_IP = Limit("refresh_ip", settings.refresh_ip_max_attempts, settings.refresh_ip_window_seconds)


def limit_by_ip(limit: Limit) -> Callable[[Request], Awaitable[None]]:
    """Зависимость FastAPI: засчитать запрос по IP клиента (за nginx — настоящий IP, см. get_request_ip)."""

    async def dependency(request: Request) -> None:
        await RateLimiter().hit(limit, get_request_ip(request) or "unknown")

    return dependency
