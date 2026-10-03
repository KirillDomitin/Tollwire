#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.log import bind

logger = logging.getLogger("api.request")

REQUEST_ID_HEADER = "X-Request-ID"
# nginx $request_id — 32 hex-символа. Чужое значение принимаем только «безопасной формы»: без пробелов,
# переводов строк и прочего, что могло бы подделать соседние поля в текстовом логе.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


class RequestContextMiddleware:
    """request_id на каждый HTTP-запрос: в контекст логов, в заголовок ответа и одна строка-итог на запрос.

    Чистый ASGI-middleware (не BaseHTTPMiddleware): контекст, выставленный здесь, гарантированно виден
    в зависимостях и обработчиках этого же запроса.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.lower().encode(), b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        status = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with bind(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                logger.exception("unhandled error", extra=self._fields(scope, 500, started))
                raise
            # Путь без query string; тела запросов и заголовки (токены, пароли) не пишем никогда.
            level = logging.WARNING if status >= 500 else logging.INFO
            logger.log(level, "request", extra=self._fields(scope, status, started))

    @staticmethod
    def _fields(scope: Scope, status: int, started: float) -> dict[str, object]:
        client = scope.get("client")
        return {
            "method": scope["method"],
            "path": scope["path"],
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "client_ip": client[0] if client else None,
        }
