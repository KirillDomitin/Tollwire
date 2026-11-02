"""Админский и Bot API привязки и уведомлений (этап 25.5) через HTTP: `/api/v1/bot-admin/me/telegram*`,
`/broadcasts/test`, `/notify-events`, `/admin-links`, `POST /api/v1/bot/admin-links`, ответ `heartbeat`, настройки.

Главное: код привязки виден один раз и не попадает ни в аудит, ни в логи; `telegram_id` админов наружу из админского
API не уходит; viewer/operator/superadmin управляют своими уведомлениями; проба себе — только при живой привязке."""

import logging
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modules.auth.models.admin import AdminRole
from tests.bot_notify_support import (
    ADMIN_LINKS_URL,
    ALL_KEYS,
    BOT_ADMIN_LINKS_URL,
    BROADCAST_TEST_URL,
    LINK_CODE_LENGTH,
    LINK_EXPIRED,
    LINK_INVALID,
    ME_CODE_URL,
    ME_EVENTS_URL,
    ME_TEST_URL,
    ME_URL,
    NOT_LINKED,
    NOTIFY_EVENTS_URL,
    PANEL_DOWN,
    SERVER_DOWN,
    TELEGRAM_TAKEN,
    TG_ADMIN,
    TG_ADMIN_2,
    audit_rows,
    default_events,
    http_bind,
    http_heartbeat,
    http_issue_code,
    http_link_admin,
    link_rows,
    notify_rows,
)
from tests.bot_support import (
    NEWS_MARKER,
    SETTINGS_URL,
    STATUS_URL,
    admin_create_key,
    audit_dump,
    bot_client,
    outbox_url,
    sql,
    tables_dump,
)
from tests.integration.conftest import AdminLogin, login_client, make_admin


@pytest.fixture
async def bot(api: httpx.AsyncClient):  # type: ignore[no-untyped-def]
    key = await admin_create_key(api)
    async with bot_client(key["key"]) as http:
        yield http


async def linked(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> str:
    return await http_link_admin(api, bot, TG_ADMIN)


# --- состояние и код ------------------------------------------------------------------------------------------------


async def test_an_admin_who_never_linked_sees_the_default_events(api: httpx.AsyncClient) -> None:
    response = await api.get(ME_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["linked"] is False and body["telegram_username"] is None and body["linked_at"] is None
    assert body["blocked_at"] is None and body["code_pending_until"] is None
    assert body["events"] == default_events()


async def test_the_code_comes_once_with_a_link_and_a_deadline(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    await http_heartbeat(bot, name="tollwire_bot")

    response = await api.post(ME_CODE_URL)

    assert response.status_code == 201
    body = response.json()
    assert len(body["code"]) == LINK_CODE_LENGTH and body["expires_at"]
    assert body["link"] == f"https://t.me/tollwire_bot?start={body['code']}"
    assert response.headers["cache-control"] == "no-store"


async def test_the_state_shows_a_pending_code_but_never_the_code(api: httpx.AsyncClient) -> None:
    code = (await http_issue_code(api))["code"]

    response = await api.get(ME_URL)

    assert response.json()["code_pending_until"] is not None and code not in response.text


async def test_a_new_code_kills_the_old_one(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    old = (await http_issue_code(api))["code"]
    new = (await http_issue_code(api))["code"]

    refused = await http_bind(bot, old)
    accepted = await http_bind(bot, new)

    assert (refused.status_code, refused.json()["reason"]) == (409, LINK_INVALID)
    assert accepted.status_code == 200


async def test_codes_are_limited_per_admin(api: httpx.AsyncClient) -> None:
    statuses = [(await api.post(ME_CODE_URL)).status_code for _ in range(11)]

    assert statuses[:10] == [201] * 10 and statuses[10] == 429


# --- привязка ботом -------------------------------------------------------------------------------------------------


async def test_the_bot_links_the_telegram_by_the_code_and_the_admin_sees_it(
    api: httpx.AsyncClient, bot: httpx.AsyncClient, admin: AdminLogin
) -> None:
    code = (await http_issue_code(api))["code"]

    response = await http_bind(bot, code, TG_ADMIN, "admin_tg")

    assert response.status_code == 200 and response.json()["admin_name"]
    state = (await api.get(ME_URL)).json()
    assert state["linked"] is True and state["telegram_username"] == "admin_tg"
    assert state["linked_at"] and state["blocked_at"] is None and state["code_pending_until"] is None
    assert str(TG_ADMIN) not in str(state)


async def test_a_code_cannot_be_used_twice(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    code = (await http_issue_code(api))["code"]
    await http_bind(bot, code, TG_ADMIN)

    again = await http_bind(bot, code, TG_ADMIN_2)

    assert (again.status_code, again.json()["reason"]) == (409, LINK_INVALID)


async def test_an_unknown_code_is_a_409_with_a_reason(bot: httpx.AsyncClient) -> None:
    response = await http_bind(bot, "z" * LINK_CODE_LENGTH)

    assert (response.status_code, response.json()["reason"]) == (409, LINK_INVALID)


async def test_an_expired_code_is_a_409_with_its_own_reason(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    code = (await http_issue_code(api))["code"]
    await sql("UPDATE admin_telegram_links SET code_expires_at = now() - interval '1 second'")

    response = await http_bind(bot, code)

    assert (response.status_code, response.json()["reason"]) == (409, LINK_EXPIRED)


async def test_a_telegram_of_another_admin_is_taken(
    api: httpx.AsyncClient, bot: httpx.AsyncClient, session: AsyncSession
) -> None:
    await linked(api, bot)
    async with login_client(await make_admin(session, AdminRole.OPERATOR)) as other:
        code = (await http_issue_code(other))["code"]
        response = await http_bind(bot, code, TG_ADMIN)

    assert (response.status_code, response.json()["reason"]) == (409, TELEGRAM_TAKEN)


@pytest.mark.parametrize("telegram_id", [0, -5, 2**63])
async def test_a_bad_telegram_id_is_422(bot: httpx.AsyncClient, telegram_id: int) -> None:
    assert (await http_bind(bot, "a" * LINK_CODE_LENGTH, telegram_id)).status_code == 422


async def test_a_missing_code_is_422(bot: httpx.AsyncClient) -> None:
    response = await bot.post(BOT_ADMIN_LINKS_URL, json={"telegram_id": TG_ADMIN})

    assert response.status_code == 422


async def test_repeated_wrong_codes_are_limited_per_key_even_for_a_valid_code(
    api: httpx.AsyncClient, bot: httpx.AsyncClient
) -> None:
    code = (await http_issue_code(api))["code"]
    for number in range(10):
        assert (await http_bind(bot, f"{number:0>{LINK_CODE_LENGTH}}")).status_code == 409

    response = await http_bind(bot, code)

    assert response.status_code == 429 and "retry-after" in response.headers


async def test_successful_links_do_not_count_as_failures(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    for _ in range(10):  # лимит выдачи кодов — 10 на админа, лимит неудач привязки — свой
        code = (await http_issue_code(api))["code"]
        assert (await http_bind(bot, code, TG_ADMIN)).status_code == 200
    assert (await api.get(ME_URL)).json()["linked"] is True


# --- события --------------------------------------------------------------------------------------------------------


async def test_the_catalog_lists_every_event_with_a_group_and_a_default(api: httpx.AsyncClient) -> None:
    response = await api.get(NOTIFY_EVENTS_URL)

    assert response.status_code == 200
    items = {item["key"]: item for item in response.json()}
    assert set(items) == set(ALL_KEYS)
    assert all(item["group"] for item in items.values())
    assert items["login_locked"]["default"] is False and items[SERVER_DOWN]["default"] is True


async def test_events_are_saved_and_shown(api: httpx.AsyncClient) -> None:
    events = {**default_events(), SERVER_DOWN: False, "login_locked": True}

    response = await api.put(ME_EVENTS_URL, json={"events": events})

    assert response.status_code == 200
    assert (await api.get(ME_URL)).json()["events"] == events
    (row,) = await audit_rows("bot_admin_link_events_updated")
    assert row[0] is not None


@pytest.mark.parametrize(
    "events",
    [{"server_exploded": True}, {SERVER_DOWN: "yes"}, {SERVER_DOWN: None}, ["server_down"], "all"],
    ids=["unknown-key", "not-a-bool", "null", "list", "string"],
)
async def test_bad_events_are_422_and_change_nothing(api: httpx.AsyncClient, events: Any) -> None:
    before = (await api.get(ME_URL)).json()["events"]

    response = await api.put(ME_EVENTS_URL, json={"events": events})

    assert response.status_code == 422
    assert (await api.get(ME_URL)).json()["events"] == before


async def test_events_without_a_body_are_422(api: httpx.AsyncClient) -> None:
    assert (await api.put(ME_EVENTS_URL, json={})).status_code == 422


# --- отвязка и проба ------------------------------------------------------------------------------------------------


async def test_unlink_is_204_and_repeatable(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    await linked(api, bot)

    first = await api.delete(ME_URL)
    second = await api.delete(ME_URL)

    assert (first.status_code, second.status_code) == (204, 204)
    assert (await api.get(ME_URL)).json()["linked"] is False
    assert await link_rows() == []
    assert len(await audit_rows("bot_admin_link_removed")) == 1


async def test_a_test_message_without_a_link_is_409_not_linked(api: httpx.AsyncClient) -> None:
    response = await api.post(ME_TEST_URL)

    assert (response.status_code, response.json()["reason"]) == (409, NOT_LINKED)


async def test_a_test_message_to_a_blocked_chat_is_409_bot_blocked(
    api: httpx.AsyncClient, bot: httpx.AsyncClient, admin: AdminLogin
) -> None:
    await linked(api, bot)
    await sql("UPDATE admin_telegram_links SET blocked_at = now()")

    response = await api.post(ME_TEST_URL)

    assert (response.status_code, response.json()["reason"]) == (409, "bot_blocked")


async def test_a_test_message_is_queued_for_the_admin_himself(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    await linked(api, bot)

    response = await api.post(ME_TEST_URL)

    assert response.status_code == 202
    (row,) = await notify_rows()
    assert str(row.id) == response.json()["outbox_id"] and row.format == "plain" and row.text
    shown = await api.get(outbox_url(row.id))
    assert shown.status_code == 200 and shown.json()["status"] == "pending"
    (event,) = await audit_rows("bot_notify_test_queued")
    assert event[0] is not None


async def test_test_messages_are_limited_per_admin(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    await linked(api, bot)

    statuses = [(await api.post(ME_TEST_URL)).status_code for _ in range(11)]

    assert statuses[:10] == [202] * 10 and statuses[10] == 429


async def test_a_test_news_goes_to_the_admin_as_html(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    await linked(api, bot)
    text = f"<b>Проба</b> {NEWS_MARKER}"

    response = await api.post(BROADCAST_TEST_URL, json={"text": text})

    assert response.status_code == 202
    (row,) = await notify_rows()
    assert (row.format, row.text, str(row.id)) == ("html", text, response.json()["outbox_id"])
    assert NEWS_MARKER not in await audit_dump()


@pytest.mark.parametrize(
    "text", ["<script>x</script>", "<b>не закрыт", "a < b", ""], ids=["tag", "open", "lt", "empty"]
)
async def test_a_test_news_with_bad_html_is_422_and_queues_nothing(
    api: httpx.AsyncClient, bot: httpx.AsyncClient, text: str
) -> None:
    await linked(api, bot)

    response = await api.post(BROADCAST_TEST_URL, json={"text": text})

    assert response.status_code == 422 and await notify_rows() == []


async def test_a_test_news_without_a_link_is_409_not_linked(api: httpx.AsyncClient) -> None:
    response = await api.post(BROADCAST_TEST_URL, json={"text": "<b>x</b>"})

    assert (response.status_code, response.json()["reason"]) == (409, NOT_LINKED)


# --- кто получает, статус, настройки, heartbeat ---------------------------------------------------------------------


async def test_the_recipients_list_never_shows_telegram_ids(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    await linked(api, bot)

    response = await api.get(ADMIN_LINKS_URL)

    assert response.status_code == 200 and len(response.json()) == 1
    assert str(TG_ADMIN) not in response.text


async def test_status_reports_the_notify_fields(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    await linked(api, bot)
    await http_heartbeat(bot, local=True)

    body = (await api.get(STATUS_URL)).json()

    assert body["last_seen_local"] is True
    assert isinstance(body["notify_capable"], bool) and body["admin_links"] == 1


async def test_heartbeat_hands_the_bot_its_chats_and_the_threshold(
    api: httpx.AsyncClient, bot: httpx.AsyncClient
) -> None:
    await linked(api, bot)

    response = await http_heartbeat(bot)

    notify = response.json()["notify"]
    assert notify["chats"] == [TG_ADMIN] and notify["panel_down_after_seconds"] > 0


async def test_heartbeat_chats_skip_admins_who_muted_panel_down(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    await linked(api, bot)
    await api.put(ME_EVENTS_URL, json={"events": {**default_events(), PANEL_DOWN: False}})

    assert (await http_heartbeat(bot)).json()["notify"]["chats"] == []


async def test_heartbeat_without_local_still_works(bot: httpx.AsyncClient) -> None:
    response = await bot.post("/api/v1/bot/heartbeat", json={"bot_username": "tollwire_bot", "version": "0.2.0"})

    assert response.status_code == 200


async def test_the_threshold_is_a_setting(api: httpx.AsyncClient, bot: httpx.AsyncClient) -> None:
    response = await api.put(SETTINGS_URL, json={"panel_down_after_seconds": 300})

    assert response.status_code == 200 and response.json()["panel_down_after_seconds"] == 300
    assert (await http_heartbeat(bot)).json()["notify"]["panel_down_after_seconds"] == 300


@pytest.mark.parametrize("value", [59, 86401, 0, -1])
async def test_a_threshold_out_of_range_is_422(api: httpx.AsyncClient, value: int) -> None:
    assert (await api.put(SETTINGS_URL, json={"panel_down_after_seconds": value})).status_code == 422


@pytest.mark.parametrize("value", [60, 86400])
async def test_the_threshold_edges_are_accepted(api: httpx.AsyncClient, value: int) -> None:
    assert (await api.put(SETTINGS_URL, json={"panel_down_after_seconds": value})).status_code == 200


# --- роли -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(AdminRole))
async def test_every_role_manages_its_own_notifications(session: AsyncSession, role: AdminRole) -> None:
    async with login_client(await make_admin(session, role)) as http:
        assert (await http.get(ME_URL)).status_code == 200
        assert (await http.post(ME_CODE_URL)).status_code == 201
        assert (await http.put(ME_EVENTS_URL, json={"events": default_events()})).status_code == 200
        assert (await http.delete(ME_URL)).status_code == 204
        assert (await http.get(NOTIFY_EVENTS_URL)).status_code == 200


async def test_a_viewer_cannot_send_a_test_news(session: AsyncSession) -> None:
    async with login_client(await make_admin(session, AdminRole.VIEWER)) as http:
        response = await http.post(BROADCAST_TEST_URL, json={"text": "<b>x</b>"})

    assert response.status_code == 403


async def test_admins_have_separate_links(
    api: httpx.AsyncClient, bot: httpx.AsyncClient, session: AsyncSession
) -> None:
    await linked(api, bot)
    async with login_client(await make_admin(session, AdminRole.VIEWER)) as other:
        assert (await other.get(ME_URL)).json()["linked"] is False
        assert (await other.delete(ME_URL)).status_code == 204
    assert (await api.get(ME_URL)).json()["linked"] is True


async def test_the_bot_key_does_not_open_the_admin_notify_api(bot: httpx.AsyncClient) -> None:
    for method, url in (("GET", ME_URL), ("POST", ME_CODE_URL), ("DELETE", ME_URL), ("GET", ADMIN_LINKS_URL)):
        assert (await bot.request(method, url)).status_code in (401, 403), url


# --- секреты --------------------------------------------------------------------------------------------------------


async def test_no_secret_reaches_the_audit_the_tables_or_the_logs(
    api: httpx.AsyncClient, bot: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    code = (await http_issue_code(api))["code"]
    await http_bind(bot, code, TG_ADMIN, "admin_tg_secret_name")
    await api.put(ME_EVENTS_URL, json={"events": default_events()})
    await api.post(ME_TEST_URL)
    await api.post(BROADCAST_TEST_URL, json={"text": f"<b>x</b> {NEWS_MARKER}"})
    await api.delete(ME_URL)

    audit = await audit_dump()
    for secret in (code, str(TG_ADMIN), "admin_tg_secret_name", NEWS_MARKER):
        assert secret not in audit, secret
        assert secret not in caplog.text, secret
    assert code not in await tables_dump("admin_telegram_links", "bot_outbox")
    assert await link_rows() == []  # отвязано выше
