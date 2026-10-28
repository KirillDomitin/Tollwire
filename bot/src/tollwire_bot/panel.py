"""PanelClient: единственный путь бота к панели (Bot API, ключ в X-Bot-Key). Только httpx и стандартная библиотека.

Исключения несут код и причину, но не тела запросов и ответов: в них бывают имя, почта, код приглашения."""

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpx

from tollwire_bot import __version__

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/bot"
# Паузы между повторами при сбое сети и 502/503/504; после последней паузы - PanelUnavailable.
RETRY_DELAYS = (0.5, 1, 2, 4)
RETRY_STATUSES = (502, 503, 504)
MAX_RATE_LIMIT_WAIT = 10  # Retry-After дольше этого бот не ждёт
CONFLICT_REASONS = frozenset(
    {
        "invite_invalid",
        "invite_expired",
        "invite_used",
        "telegram_taken",
        "email_taken",
        "config_unavailable",
        "not_ready",
        "no_access",
        "not_configured",
        "conflict",
    }
)


class PanelError(Exception):
    """Базовая ошибка обращения к панели."""


class PanelAuthError(PanelError):
    """Панель не приняла ключ бота (неверный или отозван)."""


class PanelUnavailable(PanelError):
    """Панель недоступна: сеть, таймаут, 502/503/504, сертификат не прошёл проверку."""


class PanelUnexpected(PanelError):
    """Панель ответила не по контракту (переадресация, 5xx без повтора)."""


class RateLimited(PanelError):
    """Панель просит подождать дольше, чем бот готов ждать."""


class Conflict(PanelError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Rejected(PanelError):
    """Панель отклонила ввод (422): field - какое поле."""

    def __init__(self, field: str):
        super().__init__(field)
        self.field = field


@dataclass(frozen=True)
class Registration:
    registration_id: str
    status: str


@dataclass(frozen=True)
class UserStatus:
    name: str
    valid_until: str | None
    protocol: str | None


@dataclass(frozen=True)
class ConfigLink:
    """Ссылка на страницу конфига: адрес с токеном во фрагменте и конец доступа. В лог не попадает."""

    config_url: str
    valid_until: str | None

    def __repr__(self) -> str:  # токен в адресе открывает конфиг любому: даже случайный repr в лог его не понесёт
        return "ConfigLink(...)"


@dataclass(frozen=True)
class OutboxItem:
    """Сообщение из очереди панели. Адрес ссылки (токен в нём) в repr не попадает."""

    id: str
    kind: str  # news / config
    telegram_id: int = field(repr=False)
    broadcast_id: str | None = None
    config_url: str | None = field(default=None, repr=False)
    valid_until: str | None = None


@dataclass(frozen=True)
class Claim:
    """Пачка сообщений в аренде: подтверждать её нужно под тем же lease_id и до lease_until."""

    lease_id: str
    lease_until: datetime
    items: list[OutboxItem]
    texts: dict[str, str] = field(repr=False)  # текст новости один на рассылку


@dataclass(frozen=True)
class OutboxResult:
    """Итог одной отправки. error - код из перечня панели, текста Telegram здесь нет."""

    id: str
    status: str  # sent / blocked / failed / retry
    error: str | None = None
    retry_after: int | None = None


@dataclass(frozen=True)
class AckOutcome:
    applied: list[str]
    ignored: list[str]


def _check_url(url: str) -> str:
    """https - любой адрес; http - только имя внутри сети compose (без точки): ключ не идёт открытым текстом по сети."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return url.rstrip("/")
    if parts.scheme == "http" and host and "." not in host and ":" not in host:
        return url.rstrip("/")
    raise ValueError("PANEL_URL must be https://… (plain http is allowed only for an internal host name)")


def _ssl_context(ca_file: str | None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=certifi.where())
    if ca_file:  # свой сертификат панели - в дополнение к штатному хранилищу: Let's Encrypt продолжит работать
        context.load_verify_locations(ca_file)
        # Сертификат закреплён по отпечатку (install-bot.sh), поэтому придирки Python 3.13+ к профилю X.509 (нет SKI/AKI
        # у самоподписанного и т.п.) только отрезали бы бота от своей же панели.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


class PanelClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        ca_file: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._auth_failed = False  # ERROR про отклонённый ключ пишется один раз, пока панель не ответит нормально
        self._http = httpx.AsyncClient(
            base_url=_check_url(base_url) + API_PREFIX,
            headers={"X-Bot-Key": api_key, "User-Agent": f"tollwire-bot/{__version__}"},
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,  # ключ не должен уйти на другой адрес
            verify=_ssl_context(ca_file),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- методы Bot API ----------------------------------------------------------------------------------------------

    async def heartbeat(self, bot_username: str, version: str) -> None:
        await self._request("POST", "/heartbeat", {"bot_username": bot_username, "version": version})

    async def check_invite(self, code: str) -> str:
        response = await self._request("POST", "/invites/check", {"code": code})
        status = _json(response).get("status")
        if not isinstance(status, str):
            raise PanelUnexpected("no status in the answer")
        return status

    async def register(
        self, *, code: str, telegram_id: int, telegram_username: str | None, name: str, email: str
    ) -> Registration:
        body = {
            "code": code,
            "telegram_id": telegram_id,
            "telegram_username": telegram_username,
            "name": name,
            "email": email,
        }
        return _registration(await self._request("POST", "/registrations", body))

    async def registration(self, registration_id: str) -> Registration | None:
        response = await self._request("GET", f"/registrations/{registration_id}", allow_missing=True)
        return None if response.status_code == 404 else _registration(response)

    async def user_by_telegram(self, telegram_id: int) -> UserStatus | None:
        response = await self._request("GET", f"/users/by-telegram/{telegram_id}", allow_missing=True)
        if response.status_code == 404:
            return None
        data = _json(response)
        if not isinstance(data.get("name"), str):
            raise PanelUnexpected("no name in the answer")
        return UserStatus(
            name=data["name"],
            valid_until=data.get("valid_until"),
            protocol=data.get("protocol"),
        )

    async def config_link(self, telegram_id: int) -> ConfigLink | None:
        """Новая ссылка на страницу конфига (прежняя гаснет). None - человека в панели нет; Conflict(reason) - нельзя:
        config_unavailable / not_ready / no_access / not_configured."""
        response = await self._request("POST", f"/users/by-telegram/{telegram_id}/config-link", allow_missing=True)
        if response.status_code == 404:
            return None
        data = _json(response)
        url = data.get("config_url")
        if not isinstance(url, str) or not url:
            raise PanelUnexpected("no link in the answer")
        valid_until = data.get("valid_until")
        return ConfigLink(config_url=url, valid_until=valid_until if isinstance(valid_until, str) else None)

    async def claim(self, limit: int) -> Claim:
        """Взять пачку исходящих сообщений в аренду (для ссылок на конфиг панель выпускает адрес прямо сейчас)."""
        data = _json(await self._request("POST", "/outbox/claim", {"limit": limit}))
        lease_id, until = data.get("lease_id"), data.get("lease_until")
        raw_items, raw_texts = data.get("items"), data.get("texts")
        if not isinstance(lease_id, str) or not isinstance(until, str):
            raise PanelUnexpected("no lease in the answer")
        if not isinstance(raw_items, list) or not isinstance(raw_texts, dict):
            raise PanelUnexpected("unexpected claim answer")
        try:
            lease_until = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            raise PanelUnexpected("bad lease_until") from None
        items: list[OutboxItem] = []
        for raw in raw_items:
            if not (
                isinstance(raw, dict)
                and isinstance(raw.get("id"), str)
                and isinstance(raw.get("kind"), str)
                and isinstance(raw.get("telegram_id"), int)
            ):
                raise PanelUnexpected("unexpected claim item")
            items.append(
                OutboxItem(
                    id=raw["id"],
                    kind=raw["kind"],
                    telegram_id=raw["telegram_id"],
                    broadcast_id=raw.get("broadcast_id") if isinstance(raw.get("broadcast_id"), str) else None,
                    config_url=raw.get("config_url") if isinstance(raw.get("config_url"), str) else None,
                    valid_until=raw.get("valid_until") if isinstance(raw.get("valid_until"), str) else None,
                )
            )
        texts = {str(key): value for key, value in raw_texts.items() if isinstance(value, str)}
        return Claim(lease_id=lease_id, lease_until=lease_until, items=items, texts=texts)

    async def ack(self, lease_id: str, results: list[OutboxResult]) -> AckOutcome:
        """Итоги отправки пачки. Что панель не приняла (аренда ушла другому), приходит в ignored."""
        body = {
            "lease_id": lease_id,
            "results": [
                {
                    key: value
                    for key, value in (
                        ("id", result.id),
                        ("status", result.status),
                        ("error", result.error),
                        ("retry_after", result.retry_after),
                    )
                    if value is not None
                }
                for result in results
            ],
        }
        data = _json(await self._request("POST", "/outbox/ack", body))
        applied, ignored = data.get("applied"), data.get("ignored")
        if not isinstance(applied, list) or not isinstance(ignored, list):
            raise PanelUnexpected("unexpected ack answer")
        return AckOutcome(applied=[str(item) for item in applied], ignored=[str(item) for item in ignored])

    # --- запрос ------------------------------------------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, allow_missing: bool = False
    ) -> httpx.Response:
        outage = 0  # сколько пауз по сбою уже было
        rate_limited = False
        while True:
            try:
                response = await self._http.request(method, path, json=body)
            except httpx.TransportError as exc:
                failure = type(exc).__name__
            else:
                failure = f"HTTP {response.status_code}" if response.status_code in RETRY_STATUSES else ""
            if failure:
                if outage == len(RETRY_DELAYS):
                    raise PanelUnavailable(failure)
                await self._sleep(RETRY_DELAYS[outage])
                outage += 1
                continue

            status = response.status_code
            if status == 429:
                wait = _retry_after(response)
                if rate_limited or wait is None or wait > MAX_RATE_LIMIT_WAIT:
                    raise RateLimited(f"retry after {wait}")
                rate_limited = True
                await self._sleep(wait)
                continue
            if status == 401:
                if not self._auth_failed:
                    logger.error("the panel does not accept the bot key: it is wrong or revoked")
                self._auth_failed = True
                raise PanelAuthError("bot key rejected")
            self._auth_failed = False
            if status == 404 and allow_missing:
                return response
            if status == 409:
                raise Conflict(_reason(response))
            if status == 422:
                raise Rejected(_field(response))
            if 200 <= status < 300:
                return response
            raise PanelUnexpected(f"HTTP {status}")


def _json(response: httpx.Response) -> dict[str, Any]:
    """Тело ответа как объект. Прокси или сбой панели могут вернуть 2xx с чем угодно: это PanelUnexpected, а не крах."""
    try:
        data = response.json()
    except ValueError:
        raise PanelUnexpected("the answer is not JSON") from None
    if not isinstance(data, dict):
        raise PanelUnexpected("unexpected answer")
    return data


def _registration(response: httpx.Response) -> Registration:
    data = _json(response)
    if not isinstance(data.get("registration_id"), str) or not isinstance(data.get("status"), str):
        raise PanelUnexpected("unexpected registration answer")
    return Registration(registration_id=data["registration_id"], status=data["status"])


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "")
    return int(value) if value.isdigit() else None


def _reason(response: httpx.Response) -> str:
    try:
        reason = response.json().get("reason")
    except (ValueError, AttributeError):
        return "conflict"
    return reason if reason in CONFLICT_REASONS else "conflict"


def _field(response: httpx.Response) -> str:
    """Поле из loc ответа 422. Сам detail не читаем дальше: в нём присланное значение."""
    try:
        location = response.json()["detail"][0]["loc"]
        return str(location[-1])
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
