"""Авторизация бота (этап 25.1): X-Bot-Key на каждом маршруте Bot API, отзыв, разделение с JWT админа,
rate limit, heartbeat.

Ключ бота не открывает админский API, JWT админа не открывает Bot API.
"""

from typing import Any
from uuid import UUID

import httpx
import pytest

from modules.bot.models.key import BotKey
from tests.bot_support import (
    BOT_KEY_HEADER,
    INVITES_URL,
    KEYS_URL,
    REGISTRATIONS_URL,
    TELEGRAM_ID,
    admin_create_key,
    bot_client,
    fetch,
    registration_body,
)

HEARTBEAT = {"bot_username": "tollwire_bot", "version": "0.1.0"}

# Все маршруты Bot API 25.1 с телами, которые прошли бы валидацию: 401 должен приходить раньше любой обработки.
BOT_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/api/v1/bot/heartbeat", HEARTBEAT),
    ("POST", "/api/v1/bot/invites/check", {"code": "abcdefgh12345678"}),
    ("POST", "/api/v1/bot/registrations", registration_body("abcdefgh12345678")),
    ("GET", "/api/v1/bot/registrations/00000000-0000-0000-0000-000000000000", None),
    ("GET", f"/api/v1/bot/users/by-telegram/{TELEGRAM_ID}", None),
    ("POST", f"/api/v1/bot/users/by-telegram/{TELEGRAM_ID}/config-link", None),
]
ROUTE_IDS = [
    "heartbeat",
    "invites-check",
    "registrations-post",
    "registrations-get",
    "users-by-telegram",
    "config-link",
]


async def call(http: httpx.AsyncClient, route: tuple[str, str, dict[str, Any] | None]) -> httpx.Response:
    method, path, body = route
    return await http.request(method, path, json=body)


@pytest.fixture
async def key(api: httpx.AsyncClient) -> dict[str, Any]:
    return await admin_create_key(api)


# --- каждый маршрут Bot API требует действующий ключ ---------------------------------------------------------------


@pytest.mark.parametrize("route", BOT_ROUTES, ids=ROUTE_IDS)
async def test_a_route_without_a_key_is_401(route: tuple[str, str, dict[str, Any] | None]) -> None:
    async with bot_client() as http:
        assert (await call(http, route)).status_code == 401


@pytest.mark.parametrize("route", BOT_ROUTES, ids=ROUTE_IDS)
async def test_a_route_with_an_empty_key_is_401(route: tuple[str, str, dict[str, Any] | None]) -> None:
    async with bot_client("") as http:
        assert (await call(http, route)).status_code == 401


@pytest.mark.parametrize("route", BOT_ROUTES, ids=ROUTE_IDS)
async def test_a_route_with_a_wrong_key_is_401(route: tuple[str, str, dict[str, Any] | None]) -> None:
    async with bot_client("this-is-not-a-bot-key-0123456789abcdef") as http:
        assert (await call(http, route)).status_code == 401


@pytest.mark.parametrize("route", BOT_ROUTES, ids=ROUTE_IDS)
async def test_a_route_with_a_slightly_altered_key_is_401(
    route: tuple[str, str, dict[str, Any] | None], key: dict[str, Any]
) -> None:
    altered = key["key"][:-1] + ("A" if key["key"][-1] != "A" else "B")
    async with bot_client(altered) as http:
        assert (await call(http, route)).status_code == 401


@pytest.mark.parametrize("route", BOT_ROUTES, ids=ROUTE_IDS)
async def test_a_route_with_a_revoked_key_is_401(
    route: tuple[str, str, dict[str, Any] | None], api: httpx.AsyncClient, key: dict[str, Any]
) -> None:
    assert (await api.post(f"{KEYS_URL}/{key['id']}/revoke")).status_code == 200
    async with bot_client(key["key"]) as http:
        assert (await call(http, route)).status_code == 401


@pytest.mark.parametrize("route", BOT_ROUTES, ids=ROUTE_IDS)
async def test_a_route_with_a_valid_key_is_not_401(
    route: tuple[str, str, dict[str, Any] | None], key: dict[str, Any]
) -> None:
    async with bot_client(key["key"]) as http:
        assert (await call(http, route)).status_code not in (401, 403)


@pytest.mark.parametrize("route", BOT_ROUTES, ids=ROUTE_IDS)
async def test_the_jwt_of_an_admin_does_not_open_the_bot_api(
    route: tuple[str, str, dict[str, Any] | None], api: httpx.AsyncClient
) -> None:
    token = api.headers["Authorization"]
    async with bot_client() as http:
        method, path, body = route
        as_bearer = await http.request(method, path, json=body, headers={"Authorization": token})
        as_key = await http.request(method, path, json=body, headers={BOT_KEY_HEADER: token.removeprefix("Bearer ")})
    assert as_bearer.status_code == 401 and as_key.status_code == 401


async def test_a_key_revoked_in_the_panel_stops_the_bot_at_once(api: httpx.AsyncClient, key: dict[str, Any]) -> None:
    async with bot_client(key["key"]) as bot:
        assert (await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 200
        assert (await api.post(f"{KEYS_URL}/{key['id']}/revoke")).status_code == 200
        assert (await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 401


async def test_one_revoked_key_does_not_stop_another_bot(api: httpx.AsyncClient, key: dict[str, Any]) -> None:
    other = await admin_create_key(api, "second-bot")
    assert (await api.post(f"{KEYS_URL}/{key['id']}/revoke")).status_code == 200
    async with bot_client(other["key"]) as bot:
        assert (await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 200


async def test_the_refusals_look_the_same_for_missing_wrong_and_revoked_keys(
    api: httpx.AsyncClient, key: dict[str, Any]
) -> None:
    """По ответу нельзя понять, существовал ли такой ключ."""
    assert (await api.post(f"{KEYS_URL}/{key['id']}/revoke")).status_code == 200
    answers = []
    for value in (None, "wrong-key-0123456789abcdefghijklmnop", key["key"]):
        async with bot_client(value) as http:
            response = await http.post("/api/v1/bot/heartbeat", json=HEARTBEAT)
        answers.append((response.status_code, response.json()))
    assert answers[0][0] == answers[1][0] == answers[2][0] == 401
    assert answers[1] == answers[2]


# --- ключ бота не открывает админский API --------------------------------------------------------------------------

ADMIN_ROUTES = [
    ("GET", KEYS_URL),
    ("POST", KEYS_URL),
    ("GET", INVITES_URL),
    ("POST", INVITES_URL),
    ("GET", REGISTRATIONS_URL),
    ("GET", "/api/v1/vpn-users"),
    ("GET", "/api/v1/servers"),
    ("GET", "/api/v1/audit-logs"),
    ("GET", "/api/v1/auth/me"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_a_bot_key_as_a_header_does_not_open_the_admin_api(method: str, path: str, key: dict[str, Any]) -> None:
    async with bot_client(key["key"]) as bot:
        assert (await bot.request(method, path, json={} if method == "POST" else None)).status_code == 401


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_a_bot_key_as_a_bearer_token_does_not_open_the_admin_api(
    method: str, path: str, key: dict[str, Any]
) -> None:
    async with bot_client() as http:
        response = await http.request(
            method, path, json={} if method == "POST" else None, headers={"Authorization": f"Bearer {key['key']}"}
        )
    assert response.status_code == 401


# --- heartbeat -----------------------------------------------------------------------------------------------------


async def test_heartbeat_records_the_bot_and_answers_with_an_empty_object(key: dict[str, Any]) -> None:
    async with bot_client(key["key"]) as bot:
        response = await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)
    assert response.status_code == 200 and response.json() == {}
    row: BotKey = await fetch(BotKey, UUID(key["id"]))
    assert (row.bot_username, row.last_seen_version) == ("tollwire_bot", "0.1.0")
    assert row.last_seen_at is not None


async def test_the_next_heartbeat_updates_the_version_and_the_time(key: dict[str, Any]) -> None:
    async with bot_client(key["key"]) as bot:
        await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)
        first: BotKey = await fetch(BotKey, UUID(key["id"]))
        await bot.post("/api/v1/bot/heartbeat", json={"bot_username": "renamed_bot", "version": "0.2.0"})
    second: BotKey = await fetch(BotKey, UUID(key["id"]))
    assert (second.bot_username, second.last_seen_version) == ("renamed_bot", "0.2.0")
    assert second.last_seen_at >= first.last_seen_at  # type: ignore[operator]


async def test_heartbeat_of_one_key_does_not_touch_another(api: httpx.AsyncClient, key: dict[str, Any]) -> None:
    other = await admin_create_key(api, "second-bot")
    async with bot_client(key["key"]) as bot:
        await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)
    untouched: BotKey = await fetch(BotKey, UUID(other["id"]))
    assert untouched.last_seen_at is None and untouched.bot_username is None


async def test_a_rejected_key_does_not_leave_a_heartbeat_trace(api: httpx.AsyncClient, key: dict[str, Any]) -> None:
    assert (await api.post(f"{KEYS_URL}/{key['id']}/revoke")).status_code == 200
    async with bot_client(key["key"]) as bot:
        await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)
    row: BotKey = await fetch(BotKey, UUID(key["id"]))
    assert row.last_seen_at is None


@pytest.mark.parametrize(
    "body",
    [{}, {"bot_username": "tollwire_bot"}, {"version": "0.1.0"}, {"bot_username": "x" * 1000, "version": "0.1.0"}],
)
async def test_a_malformed_heartbeat_is_422(key: dict[str, Any], body: dict[str, Any]) -> None:
    async with bot_client(key["key"]) as bot:
        assert (await bot.post("/api/v1/bot/heartbeat", json=body)).status_code == 422


# --- rate limit ----------------------------------------------------------------------------------------------------

PROBE_LIMIT = 1500  # больше любого разумного лимита в минуту; упереться в потолок значит, что лимита нет


async def hits_until_limited(bot: httpx.AsyncClient, make_request: Any) -> httpx.Response | None:
    for attempt in range(PROBE_LIMIT):
        response = await make_request(bot, attempt)
        if response.status_code == 429:
            return response  # type: ignore[no-any-return]
        assert response.status_code != 401
    return None


async def test_heartbeats_are_limited_per_key_with_retry_after(api: httpx.AsyncClient, key: dict[str, Any]) -> None:
    other = await admin_create_key(api, "second-bot")

    async def heartbeat(bot: httpx.AsyncClient, attempt: int) -> httpx.Response:
        return await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)

    async with bot_client(key["key"]) as bot, bot_client(other["key"]) as calm_bot:
        limited = await hits_until_limited(bot, heartbeat)
        assert limited is not None, "the Bot API must be rate limited per key"
        assert limited.json() == {"detail": "Too many requests"}
        assert int(limited.headers["Retry-After"]) >= 1
        assert (await calm_bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 200  # лимит по ключу


async def test_registrations_have_their_own_stricter_limit(api: httpx.AsyncClient, key: dict[str, Any]) -> None:
    async def attempt_registration(bot: httpx.AsyncClient, attempt: int) -> httpx.Response:
        return await bot.post(
            "/api/v1/bot/registrations",
            json=registration_body("no-such-code-at-all", telegram_id=TELEGRAM_ID + attempt),
        )

    async with bot_client(key["key"]) as bot:
        limited = await hits_until_limited(bot, attempt_registration)
        assert limited is not None, "registrations must be limited per key"
        assert int(limited.headers["Retry-After"]) >= 1
        # Отдельное правило: до общего лимита Bot API далеко, heartbeat бота ещё проходит.
        assert (await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 200


async def test_a_limited_key_is_still_a_key_and_the_panel_keeps_working(
    api: httpx.AsyncClient, key: dict[str, Any]
) -> None:
    async def heartbeat(bot: httpx.AsyncClient, attempt: int) -> httpx.Response:
        return await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)

    async with bot_client(key["key"]) as bot:
        assert await hits_until_limited(bot, heartbeat) is not None
    assert (await api.get(KEYS_URL)).status_code == 200  # админский API не делит счётчик с ботом
