"""Контракт «бот - панель», этап 25.5: настоящий `PanelClient` против настоящего API.

`heartbeat -> NotifyConfig` (чаты и порог), `claim(limit, kinds)` со строками `notify`, `admin_link(...)` и его причины
409, 401, 429. Фейк `bot/tests/fakes.py` обязан совпадать с этим контрактом."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from main import app
from modules.auth.models.admin import AdminRole
from tests.bot_client_support import Conflict, PanelAuthError, PanelClient, RateLimited, Rejected, outbox_result
from tests.bot_notify_support import (
    LINK_CODE_LENGTH,
    LINK_EXPIRED,
    LINK_INVALID,
    ME_URL,
    TELEGRAM_TAKEN,
    TG_ADMIN,
    TG_ADMIN_2,
    admin_login,
    http_issue_code,
    queue_notify,
)
from tests.bot_support import SETTINGS_URL, admin_create_key, audit_dump, sql
from tests.integration.conftest import login_client

BOT_NAME = "tollwire_contract_bot"


@pytest.fixture
async def client(api: httpx.AsyncClient) -> AsyncIterator[PanelClient]:
    key = await admin_create_key(api)
    panel = PanelClient("http://testserver", key["key"], transport=httpx.ASGITransport(app=app))
    try:
        yield panel
    finally:
        await panel.aclose()


async def link(client: PanelClient, api: httpx.AsyncClient, telegram_id: int = TG_ADMIN) -> Any:
    code = (await http_issue_code(api))["code"]
    return await client.admin_link(code, telegram_id, "admin_tg")


# --- admin_link -----------------------------------------------------------------------------------------------------


async def test_admin_link_returns_the_admin_name(client: PanelClient, api: httpx.AsyncClient) -> None:
    result = await link(client, api)

    assert result.admin_name
    assert (await api.get(ME_URL)).json()["linked"] is True


async def test_admin_link_without_a_username_works(client: PanelClient, api: httpx.AsyncClient) -> None:
    code = (await http_issue_code(api))["code"]

    assert (await client.admin_link(code, TG_ADMIN, None)).admin_name


@pytest.mark.parametrize(
    ("setup", "reason"),
    [("unknown", LINK_INVALID), ("expired", LINK_EXPIRED), ("taken", TELEGRAM_TAKEN)],
)
async def test_every_admin_link_reason_reaches_the_client(
    client: PanelClient, api: httpx.AsyncClient, setup: str, reason: str
) -> None:
    code = "q" * LINK_CODE_LENGTH
    if setup == "expired":
        code = (await http_issue_code(api))["code"]
        await sql("UPDATE admin_telegram_links SET code_expires_at = now() - interval '1 second'")
    if setup == "taken":
        await link(client, api, TG_ADMIN)
        # у другого админа свой код, а Telegram уже занят первым
        async with login_client(await admin_login(AdminRole.OPERATOR)) as other:
            code = (await http_issue_code(other))["code"]

    with pytest.raises(Conflict) as caught:
        await client.admin_link(code, TG_ADMIN, "admin_tg")

    assert caught.value.reason == reason


async def test_admin_link_with_a_bad_telegram_id_names_the_field(client: PanelClient) -> None:
    with pytest.raises(Rejected) as caught:
        await client.admin_link("a" * LINK_CODE_LENGTH, 0, None)

    assert caught.value.field == "telegram_id"


async def test_admin_link_with_a_wrong_key_is_an_auth_error() -> None:
    panel = PanelClient("http://testserver", "twb_" + "x" * 43, transport=httpx.ASGITransport(app=app))
    try:
        with pytest.raises(PanelAuthError):
            await panel.admin_link("a" * LINK_CODE_LENGTH, TG_ADMIN, None)
    finally:
        await panel.aclose()


async def test_too_many_wrong_codes_end_in_rate_limited(client: PanelClient) -> None:
    for number in range(10):
        with pytest.raises(Conflict):
            await client.admin_link(f"{number:0>{LINK_CODE_LENGTH}}", TG_ADMIN, None)

    with pytest.raises(RateLimited):
        await client.admin_link("b" * LINK_CODE_LENGTH, TG_ADMIN, None)


# --- heartbeat ------------------------------------------------------------------------------------------------------


async def test_heartbeat_gives_the_bot_the_notify_config(client: PanelClient, api: httpx.AsyncClient) -> None:
    await link(client, api)
    await api.put(SETTINGS_URL, json={"panel_down_after_seconds": 420})

    config = await client.heartbeat(BOT_NAME, "0.3.0", local=False)

    assert config.chats == [TG_ADMIN] and config.panel_down_after_seconds == 420


async def test_heartbeat_with_nobody_linked_gives_no_chats(client: PanelClient) -> None:
    config = await client.heartbeat(BOT_NAME, "0.3.0", local=True)

    assert config.chats == [] and config.panel_down_after_seconds >= 60


# --- claim ----------------------------------------------------------------------------------------------------------


async def test_claim_with_kinds_gets_a_notify_with_text_and_format_and_ack_closes_it(
    client: PanelClient, api: httpx.AsyncClient
) -> None:
    await link(client, api)
    row = (await sql_link_id())
    outbox_id = await queue_notify(row, "Сервер alpha недоступен")

    claim = await client.claim(10, kinds=["news", "config", "notify"])

    (item,) = claim.items
    assert (item.id, item.kind, item.telegram_id) == (str(outbox_id), "notify", TG_ADMIN)
    assert (item.text, item.format) == ("Сервер alpha недоступен", "plain")
    assert "Сервер alpha" not in repr(item) and "Сервер alpha" not in repr(claim)
    outcome = await client.ack(claim.lease_id, [outbox_result(item.id, "sent")])
    assert outcome.applied == [str(outbox_id)]


async def test_claim_without_kinds_does_not_see_notify(client: PanelClient, api: httpx.AsyncClient) -> None:
    await link(client, api)
    await queue_notify(await sql_link_id())

    assert (await client.claim(10)).items == []


async def test_ack_blocked_of_a_notify_marks_the_admin_link(client: PanelClient, api: httpx.AsyncClient) -> None:
    await link(client, api)
    outbox_id = await queue_notify(await sql_link_id())
    claim = await client.claim(10, kinds=["notify"])

    await client.ack(claim.lease_id, [outbox_result(outbox_id, "blocked", error="blocked")])

    assert (await api.get(ME_URL)).json()["blocked_at"] is not None


async def test_the_whole_dialog_leaves_no_code_in_the_audit(client: PanelClient, api: httpx.AsyncClient) -> None:
    code = (await http_issue_code(api))["code"]
    await client.admin_link(code, TG_ADMIN_2, "admin_tg")
    await client.heartbeat(BOT_NAME, "0.3.0", local=False)

    audit = await audit_dump()
    assert code not in audit and str(TG_ADMIN_2) not in audit


async def sql_link_id() -> Any:
    from tests.bot_support import sql_rows

    return (await sql_rows("SELECT id FROM admin_telegram_links"))[0][0]
