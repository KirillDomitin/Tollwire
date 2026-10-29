"""Админский API страницы «Бот» (этап 25.4): состояние бота, настройки, протоколы, получатели, списки с курсором,
рассылки, ссылка одному человеку, опрос состояния сообщения, поля Telegram у пользователя.

Через HTTP, с настоящим входом. Права: чтение - `bot:read`, записи (кроме ключей) - `bot:manage`; viewer читает
и не пишет. Допущения об именах полей - в блоке 25.4 файла `tests/bot_support.py`."""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from infrastructure.workers.worker import JobWorker
from modules.auth.models.admin import AdminRole
from tests.bot_support import (
    AUDIENCE_URL,
    BOT_BLOCKED,
    BROADCASTS_URL,
    CONFIG,
    CONFIG_UNAVAILABLE,
    NEWS_MARKER,
    NEWS_TEXT,
    NO_ACCESS,
    NO_TELEGRAM,
    NOT_CONFIGURED,
    PROTOCOLS_URL,
    RECIPIENTS_URL,
    SECOND_TELEGRAM_ID,
    SETTINGS_URL,
    STATUS_URL,
    TARGET_ALL,
    TARGET_PROTOCOL,
    TARGET_SELECTED,
    TELEGRAM_ID,
    ack_result,
    add_people,
    add_person,
    admin_broadcast,
    admin_create_broadcast,
    admin_create_invite,
    admin_create_key,
    audience_body,
    audit_dump,
    bot_client,
    broadcast_body,
    broadcast_url,
    http_ack,
    http_activate,
    http_claim,
    outbox_url,
    set_public_address,
    sql,
    sql_rows,
    user_config_link_url,
)
from tests.fixtures import World
from tests.integration.conftest import login_client, make_admin

BROADCAST_FIELDS = {
    "id",
    "kind",
    "target",
    "protocol_id",
    "created_at",
    "cancelled_at",
    "finished_at",
    "total",
    "sent",
    "blocked",
    "failed",
    "skipped",
    "cancelled",
    "state",
}


@dataclass
class Scene:
    api: httpx.AsyncClient
    bot: httpx.AsyncClient
    key: dict[str, Any]
    protocol_id: UUID
    worker: JobWorker


@pytest.fixture
async def scene(api: httpx.AsyncClient, world: World, worker: JobWorker) -> AsyncIterator[Scene]:
    _, protocol, _ = await world.server()
    assert protocol is not None
    key = await admin_create_key(api)
    async with bot_client(key["key"]) as bot:
        yield Scene(api, bot, key, protocol.id, worker)


async def user_id_of(telegram_id: int = TELEGRAM_ID) -> str:
    return str((await sql_rows("SELECT id FROM vpn_users WHERE telegram_id = :t", t=telegram_id))[0][0])


async def active_person(scene: Scene, **fields: Any) -> str:
    """Человек, зарегистрированный через бота и получивший клиента; возвращает id в панели."""
    await http_activate(scene.api, scene.bot, scene.protocol_id, scene.worker, **fields)
    return await user_id_of(fields.get("telegram_id", TELEGRAM_ID))


async def with_role(session: AsyncSession, role: AdminRole) -> Any:
    return await make_admin(session, role)


async def get_json(api: httpx.AsyncClient, url: str, **params: Any) -> Any:
    response = await api.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- состояние бота ------------------------------------------------------------------------------------------------


async def test_status_without_keys_says_so(api: httpx.AsyncClient) -> None:
    body = await get_json(api, STATUS_URL)

    assert body["has_keys"] is False and body["active_keys"] == 0
    assert body["bot_username"] is None and body["last_seen_at"] is None and body["last_claim_at"] is None
    assert body["outbox"] == {"pending": 0, "claimed": 0}


async def test_status_after_a_heartbeat_shows_the_name_version_and_that_the_bot_is_alive(scene: Scene) -> None:
    await scene.bot.post("/api/v1/bot/heartbeat", json={"bot_username": "tollwire_bot", "version": "0.2.0"})

    body = await get_json(scene.api, STATUS_URL)

    assert body["has_keys"] is True and body["active_keys"] == 1 and body["stale"] is False
    assert body["bot_username"] == "tollwire_bot" and body["last_seen_version"] == "0.2.0"
    assert abs(datetime.fromisoformat(body["last_seen_at"]) - datetime.now(timezone.utc)) < timedelta(minutes=1)


@pytest.mark.parametrize(("silent_for", "stale"), [(100, False), (150, True)])
async def test_status_calls_the_bot_stale_after_two_minutes_without_a_heartbeat(
    scene: Scene, silent_for: int, stale: bool
) -> None:
    await scene.bot.post("/api/v1/bot/heartbeat", json={"bot_username": "tollwire_bot", "version": "0.2.0"})
    await sql("UPDATE bot_keys SET last_seen_at = now() - make_interval(secs => :s)", s=float(silent_for))

    assert (await get_json(scene.api, STATUS_URL))["stale"] is stale


async def test_status_of_a_key_that_never_reported_is_stale(scene: Scene) -> None:
    assert (await get_json(scene.api, STATUS_URL))["stale"] is True


async def test_status_remembers_when_the_bot_last_took_the_queue(scene: Scene) -> None:
    assert (await get_json(scene.api, STATUS_URL))["last_claim_at"] is None

    await http_claim(scene.bot)

    last = (await get_json(scene.api, STATUS_URL))["last_claim_at"]
    assert abs(datetime.fromisoformat(last) - datetime.now(timezone.utc)) < timedelta(minutes=1)


async def test_status_counts_the_open_queue(session: AsyncSession, scene: Scene) -> None:
    await add_people(session, 3)
    await admin_create_broadcast(scene.api)
    await http_claim(scene.bot, 1)

    assert (await get_json(scene.api, STATUS_URL))["outbox"] == {"pending": 2, "claimed": 1}


async def test_a_revoked_key_still_counts_as_a_key_but_not_as_an_active_one(scene: Scene) -> None:
    await scene.bot.post("/api/v1/bot/heartbeat", json={"bot_username": "tollwire_bot", "version": "0.2.0"})
    assert (await scene.api.post(f"/api/v1/bot-admin/keys/{scene.key['id']}/revoke")).status_code == 200

    body = await get_json(scene.api, STATUS_URL)

    assert body["has_keys"] is True and body["active_keys"] == 0 and body["bot_username"] is None


async def test_status_shows_the_name_of_the_most_recently_seen_active_key(scene: Scene, api: httpx.AsyncClient) -> None:
    other = await admin_create_key(api, "second-bot")
    await scene.bot.post("/api/v1/bot/heartbeat", json={"bot_username": "old_bot", "version": "0.2.0"})
    await sql("UPDATE bot_keys SET last_seen_at = now() - interval '10 minutes' WHERE name = 'test-bot'")
    async with bot_client(other["key"]) as second:
        await second.post("/api/v1/bot/heartbeat", json={"bot_username": "new_bot", "version": "0.2.0"})

    assert (await get_json(api, STATUS_URL))["bot_username"] == "new_bot"


# --- настройки -----------------------------------------------------------------------------------------------------


async def test_settings_start_with_the_agreed_defaults(api: httpx.AsyncClient) -> None:
    body = await get_json(api, SETTINGS_URL)

    assert body["default_protocol_id"] is None and body["default_days"] == 30
    assert body["invite_valid_days"] == 7 and body["panel_down_after_seconds"] == 300


async def test_settings_can_be_changed_and_read_back(scene: Scene) -> None:
    body = {"default_protocol_id": str(scene.protocol_id), "default_days": 90, "invite_valid_days": 14}

    response = await scene.api.put(SETTINGS_URL, json=body)

    assert response.status_code == 200, response.text
    assert {key: response.json()[key] for key in body} == body
    assert {key: (await get_json(scene.api, SETTINGS_URL))[key] for key in body} == body


@pytest.mark.parametrize(
    "fields",
    [
        {"default_days": 0},
        {"default_days": 3651},
        {"invite_valid_days": 0},
        {"invite_valid_days": 366},
        {"default_protocol_id": "not-a-uuid"},
        {"default_days": "many"},
    ],
    ids=["days 0", "days 3651", "valid 0", "valid 366", "protocol not a uuid", "days not a number"],
)
async def test_invalid_settings_are_422_and_change_nothing(api: httpx.AsyncClient, fields: dict[str, Any]) -> None:
    body = {"default_protocol_id": None, "default_days": 30, "invite_valid_days": 7, **fields}

    assert (await api.put(SETTINGS_URL, json=body)).status_code == 422
    assert (await get_json(api, SETTINGS_URL))["default_days"] == 30


async def test_a_default_protocol_that_does_not_exist_is_404(api: httpx.AsyncClient) -> None:
    body = {"default_protocol_id": str(uuid4()), "default_days": 30, "invite_valid_days": 7}

    assert (await api.put(SETTINGS_URL, json=body)).status_code == 404


async def test_changing_settings_is_audited_with_was_and_became(scene: Scene) -> None:
    await scene.api.put(SETTINGS_URL, json={"default_protocol_id": None, "default_days": 60, "invite_valid_days": 7})

    logs = (await scene.api.get("/api/v1/audit-logs", params={"action": "bot_settings_updated"})).json()

    assert len(logs) == 1 and logs[0]["entity_type"] == "bot_settings"
    assert logs[0]["metadata"] == {"changes": {"default_days": [30, 60]}}


async def test_an_invite_without_details_uses_the_settings_over_http(scene: Scene) -> None:
    await scene.api.put(
        SETTINGS_URL,
        json={"default_protocol_id": str(scene.protocol_id), "default_days": 14, "invite_valid_days": 3},
    )

    response = await scene.api.post("/api/v1/bot-admin/invites", json={})

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["protocol_id"] == str(scene.protocol_id) and created["days"] == 14
    expires = datetime.fromisoformat(created["expires_at"])
    assert abs(expires - (datetime.now(timezone.utc) + timedelta(days=3))) < timedelta(minutes=5)


async def test_an_invite_without_a_protocol_anywhere_is_409_with_the_reason(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/v1/bot-admin/invites", json={"days": 5})

    assert response.status_code == 409 and response.json()["reason"] == "no_default_protocol"


# --- протоколы и получатели ----------------------------------------------------------------------------------------


async def test_protocols_lists_what_an_invite_or_a_broadcast_can_target(scene: Scene) -> None:
    body = await get_json(scene.api, PROTOCOLS_URL)

    (item,) = body
    assert item["id"] == str(scene.protocol_id) and item["server_name"] and item["type"]


async def test_protocols_is_empty_without_servers(api: httpx.AsyncClient) -> None:
    assert await get_json(api, PROTOCOLS_URL) == []


async def test_recipients_are_the_people_with_telegram_and_show_who_blocked_the_bot(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    await add_person(session, 5550000001, "Ivan Petrov", telegram_username="ivan_tg")
    await add_person(session, 5550000002, "Anna Blocked", blocked=True)
    await add_person(session, None, "Nobody")

    body = await get_json(api, RECIPIENTS_URL)

    by_name = {item["username"]: item for item in body}
    assert set(by_name) == {"Ivan Petrov", "Anna Blocked"}
    assert by_name["Ivan Petrov"]["telegram_username"] == "ivan_tg" and by_name["Ivan Petrov"]["bot_blocked_at"] is None
    assert by_name["Anna Blocked"]["bot_blocked_at"] is not None
    assert {"id", "username", "telegram_username", "bot_blocked_at"} <= set(body[0])
    assert "telegram_id" not in body[0] and "5550000001" not in str(body)


async def test_recipients_are_searched_by_name_and_by_telegram_name_in_any_case(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    await add_person(session, 5550000001, "Ivan Petrov", telegram_username="vanya_tg")
    await add_person(session, 5550000002, "Olga Smith", telegram_username="olga_s")

    assert [item["username"] for item in await get_json(api, RECIPIENTS_URL, search="PETR")] == ["Ivan Petrov"]
    assert [item["username"] for item in await get_json(api, RECIPIENTS_URL, search="olga_S")] == ["Olga Smith"]
    assert await get_json(api, RECIPIENTS_URL, search="nobody-like-this") == []


async def test_recipients_respect_the_limit(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_people(session, 5)

    assert len(await get_json(api, RECIPIENTS_URL, limit=2)) == 2


async def test_a_search_with_sql_wildcards_matches_them_literally(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    await add_person(session, 5550000001, "Ivan Petrov")

    assert await get_json(api, RECIPIENTS_URL, search="%") == []
    assert await get_json(api, RECIPIENTS_URL, search="_van") == []


# --- списки приглашений и регистраций ------------------------------------------------------------------------------


def cursor_of(row: dict[str, Any]) -> dict[str, Any]:
    return {"before_created_at": row["created_at"], "before_id": row["id"]}


@pytest.mark.parametrize("url", ["/api/v1/bot-admin/invites", "/api/v1/bot-admin/registrations", BROADCASTS_URL])
async def test_the_limit_of_a_list_is_between_1_and_100(api: httpx.AsyncClient, url: str) -> None:
    assert (await api.get(url, params={"limit": 0})).status_code == 422
    assert (await api.get(url, params={"limit": 101})).status_code == 422
    assert (await api.get(url, params={"limit": 100})).status_code == 200


@pytest.mark.parametrize("url", ["/api/v1/bot-admin/invites", "/api/v1/bot-admin/registrations", BROADCASTS_URL])
async def test_a_cursor_needs_both_halves(api: httpx.AsyncClient, url: str) -> None:
    assert (await api.get(url, params={"before_id": str(uuid4())})).status_code == 422
    assert (await api.get(url, params={"before_created_at": datetime.now(timezone.utc).isoformat()})).status_code == 422


async def test_invites_are_listed_newest_first_page_by_page_without_overlap(scene: Scene) -> None:
    for note in ("first", "second", "third"):
        await admin_create_invite(scene.api, scene.protocol_id, note=note)

    first_page = await get_json(scene.api, "/api/v1/bot-admin/invites", limit=2)
    second_page = await get_json(scene.api, "/api/v1/bot-admin/invites", limit=2, **cursor_of(first_page[-1]))

    assert [item["note"] for item in first_page] == ["third", "second"]
    assert [item["note"] for item in second_page] == ["first"]


async def test_an_invite_shows_who_used_it(scene: Scene) -> None:
    unused = await admin_create_invite(scene.api, scene.protocol_id, note="unused")
    await active_person(scene, name="Ivan Petrov")

    by_note = {item["note"]: item for item in await get_json(scene.api, "/api/v1/bot-admin/invites")}

    used = next(item for item in by_note.values() if item["status"] == "used")
    assert used["used_by_name"] == "Ivan Petrov" and used["used_by_user_id"] == await user_id_of()
    assert by_note["unused"]["used_by_user_id"] is None and by_note["unused"]["used_by_name"] is None
    assert unused["code"] not in str(by_note)  # код приглашения в списке не показывается


async def test_registrations_are_paged_the_same_way_and_keep_the_status_filter(scene: Scene) -> None:
    await active_person(scene, name="Ivan Petrov")
    await active_person(scene, name="Maria Sidorova", telegram_id=SECOND_TELEGRAM_ID, email="maria@example.org")

    page = await get_json(scene.api, "/api/v1/bot-admin/registrations", limit=1)
    rest = await get_json(scene.api, "/api/v1/bot-admin/registrations", limit=1, **cursor_of(page[0]))
    active = await get_json(scene.api, "/api/v1/bot-admin/registrations", status="active")
    failed = await get_json(scene.api, "/api/v1/bot-admin/registrations", status="failed")

    assert len(page) == len(rest) == 1 and page[0]["id"] != rest[0]["id"]
    assert len(active) == 2 and failed == []


# --- рассылки ------------------------------------------------------------------------------------------------------


async def test_creating_a_broadcast_is_201_and_shows_how_many_people_it_will_reach(
    scene: Scene, session: AsyncSession
) -> None:
    await add_people(session, 3)
    await add_person(session, 5550000099, "Blocked", blocked=True)

    response = await admin_broadcast(scene.api)

    assert response.status_code == 201, response.text
    body = response.json()
    assert BROADCAST_FIELDS <= set(body)
    assert (body["kind"], body["target"], body["total"], body["state"]) == ("news", TARGET_ALL, 3, "running")
    assert (body["sent"], body["blocked"], body["failed"], body["skipped"], body["cancelled"]) == (0, 0, 0, 0, 0)
    assert body["cancelled_at"] is None and body["finished_at"] is None


async def test_the_same_request_id_returns_the_same_broadcast(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 2)
    request = broadcast_body()

    first = await scene.api.post(BROADCASTS_URL, json=request)
    second = await scene.api.post(BROADCASTS_URL, json=request)

    assert first.status_code == 201 and second.status_code in (200, 201)
    assert first.json()["id"] == second.json()["id"] and second.json()["total"] == 2
    assert len(await get_json(scene.api, BROADCASTS_URL)) == 1
    assert (await get_json(scene.api, "/api/v1/bot-admin/status"))["outbox"]["pending"] == 2


async def test_a_broadcast_for_selected_people_and_for_a_protocol(scene: Scene, session: AsyncSession) -> None:
    people = await add_people(session, 3)
    await active_person(scene)

    selected = await admin_create_broadcast(scene.api, target=TARGET_SELECTED, user_ids=[people[0].id, people[1].id])
    by_protocol = await admin_create_broadcast(scene.api, target=TARGET_PROTOCOL, protocol_id=scene.protocol_id)

    assert selected["total"] == 2 and by_protocol["total"] == 1
    assert by_protocol["protocol_id"] == str(scene.protocol_id)


async def test_bad_news_html_is_422_with_the_code_and_the_position_and_without_the_text(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    await add_people(session, 1)

    response = await admin_broadcast(api, text=f"{NEWS_MARKER} <script>alert(1)</script>")

    assert response.status_code == 422
    (item,) = response.json()["detail"]
    assert item["type"] == "telegram_html_tag" and item["loc"][-1] == "text"
    assert isinstance(item["ctx"]["position"], int) and "input" not in item
    assert NEWS_MARKER not in response.text and "alert(1)" not in response.text
    assert await get_json(api, BROADCASTS_URL) == []


INVALID_BROADCASTS: list[tuple[dict[str, Any], str]] = [
    ({"text": "a" * 4097}, "text longer than 4096 units"),
    ({"text": "   "}, "text of spaces"),
    ({"kind": "digest"}, "unknown kind"),
    ({"target": "everybody"}, "unknown target"),
    ({"kind": CONFIG, "target": TARGET_ALL}, "config links to everybody"),
    ({"target": TARGET_SELECTED}, "selected without people"),
    ({"target": TARGET_SELECTED, "user_ids": []}, "selected with an empty list"),
    ({"target": TARGET_PROTOCOL}, "protocol without a protocol id"),
]


@pytest.mark.parametrize(("fields", "why"), INVALID_BROADCASTS, ids=[why for _, why in INVALID_BROADCASTS])
async def test_invalid_broadcast_input_is_422_and_queues_nothing(
    api: httpx.AsyncClient, session: AsyncSession, fields: dict[str, Any], why: str
) -> None:
    await add_people(session, 2)

    assert (await admin_broadcast(api, **fields)).status_code == 422
    assert await get_json(api, BROADCASTS_URL) == []


async def test_more_than_a_thousand_selected_people_is_422(api: httpx.AsyncClient) -> None:
    ids = [uuid4() for _ in range(1001)]

    assert (await admin_broadcast(api, target=TARGET_SELECTED, user_ids=ids)).status_code == 422


async def test_a_request_without_request_id_is_422(api: httpx.AsyncClient) -> None:
    body = broadcast_body()
    del body["request_id"]

    assert (await api.post(BROADCASTS_URL, json=body)).status_code == 422


async def test_config_links_without_a_panel_address_are_409_not_configured(
    api: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    people = await add_people(session, 1)
    set_public_address(monkeypatch, "", domain="")

    response = await admin_broadcast(api, kind=CONFIG, target=TARGET_SELECTED, user_ids=[people[0].id])

    assert response.status_code == 409 and response.json()["reason"] == NOT_CONFIGURED
    assert await get_json(api, BROADCASTS_URL) == []


async def test_more_recipients_than_the_limit_is_422_and_queues_nothing(
    api: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_people(session, 4)
    monkeypatch.setattr(settings, "bot_broadcast_max_recipients", 3)

    assert (await admin_broadcast(api)).status_code == 422
    assert await get_json(api, BROADCASTS_URL) == []


async def test_audience_counts_without_creating_anything(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_people(session, 2)
    await add_person(session, 5550000098, "Blocked", blocked=True)
    await add_person(session, None, "No telegram")

    response = await api.post(AUDIENCE_URL, json=audience_body())

    assert response.status_code == 200, response.text
    assert response.json() == {"recipients": 2, "without_telegram": 1, "blocked": 1}
    assert await get_json(api, BROADCASTS_URL) == []
    assert "bot_broadcast_created" not in await audit_dump()


async def test_audience_of_selected_people_and_of_a_protocol(scene: Scene, session: AsyncSession) -> None:
    people = await add_people(session, 2)
    await active_person(scene)

    selected = await scene.api.post(AUDIENCE_URL, json=audience_body(target=TARGET_SELECTED, user_ids=[people[0].id]))
    by_protocol = await scene.api.post(
        AUDIENCE_URL, json=audience_body(target=TARGET_PROTOCOL, protocol_id=scene.protocol_id)
    )

    assert selected.json()["recipients"] == 1 and by_protocol.json()["recipients"] == 1


async def test_audience_with_invalid_input_is_422(api: httpx.AsyncClient) -> None:
    assert (await api.post(AUDIENCE_URL, json=audience_body(target=TARGET_SELECTED))).status_code == 422
    assert (await api.post(AUDIENCE_URL, json={"kind": "digest", "target": "all"})).status_code == 422


async def test_a_broadcast_is_read_by_id_and_lists_newest_first(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 2)
    first = await admin_create_broadcast(scene.api, text="<b>one</b>")
    second = await admin_create_broadcast(scene.api, text="<b>two</b>")

    one = await get_json(scene.api, broadcast_url(first["id"]))
    listed = await get_json(scene.api, BROADCASTS_URL)

    assert one["id"] == first["id"] and BROADCAST_FIELDS <= set(one)
    assert [item["id"] for item in listed] == [second["id"], first["id"]]


async def test_broadcasts_are_paged_by_the_cursor(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 1)
    created = [await admin_create_broadcast(scene.api) for _ in range(3)]

    page = await get_json(scene.api, BROADCASTS_URL, limit=2)
    rest = await get_json(scene.api, BROADCASTS_URL, limit=2, **cursor_of(page[-1]))

    assert [item["id"] for item in page] == [created[2]["id"], created[1]["id"]]
    assert [item["id"] for item in rest] == [created[0]["id"]]


async def test_an_unknown_broadcast_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.get(broadcast_url(uuid4()))).status_code == 404
    assert (await api.post(broadcast_url(uuid4(), "/cancel"))).status_code == 404


async def test_the_state_follows_the_run_from_running_to_finished(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 2)
    created = await admin_create_broadcast(scene.api)
    claimed = (await http_claim(scene.bot)).json()

    assert (await get_json(scene.api, broadcast_url(created["id"])))["state"] == "running"
    await http_ack(scene.bot, claimed["lease_id"], [ack_result(item["id"]) for item in claimed["items"]])

    done = await get_json(scene.api, broadcast_url(created["id"]))
    assert done["state"] == "finished" and done["sent"] == 2 and done["finished_at"] is not None


async def test_a_broadcast_whose_last_open_row_vanished_with_a_deleted_person_is_finished(
    scene: Scene, session: AsyncSession
) -> None:
    people = await add_people(session, 2)
    created = await admin_create_broadcast(scene.api)
    claimed = (await http_claim(scene.bot, 1)).json()
    await http_ack(scene.bot, claimed["lease_id"], [ack_result(item["id"]) for item in claimed["items"]])
    gone = next(person for person in people if person.telegram_id != claimed["items"][0]["telegram_id"])

    await sql("DELETE FROM vpn_users WHERE id = :id", id=gone.id)  # у него оставалась открытая строка

    assert (await get_json(scene.api, broadcast_url(created["id"])))["state"] == "finished"
    assert [item["state"] for item in await get_json(scene.api, BROADCASTS_URL)] == ["finished"]


async def test_cancelling_a_finished_broadcast_changes_nothing(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 1)
    created = await admin_create_broadcast(scene.api)
    claimed = (await http_claim(scene.bot)).json()
    await http_ack(scene.bot, claimed["lease_id"], [ack_result(item["id"]) for item in claimed["items"]])

    response = await scene.api.post(broadcast_url(created["id"], "/cancel"))

    assert (
        response.status_code == 200
        and response.json()["state"] == "finished"
        and response.json()["cancelled_at"] is None
    )
    assert (await scene.api.get("/api/v1/audit-logs", params={"action": "bot_broadcast_cancelled"})).json() == []


async def test_cancelling_a_broadcast_is_200_and_a_repeat_changes_nothing(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 4)
    created = await admin_create_broadcast(scene.api)
    await http_claim(scene.bot, 1)

    first = await scene.api.post(broadcast_url(created["id"], "/cancel"))
    second = await scene.api.post(broadcast_url(created["id"], "/cancel"))

    assert first.status_code == second.status_code == 200
    assert first.json()["state"] == "cancelled" and first.json()["cancelled"] == 3 and first.json()["cancelled_at"]
    assert second.json()["cancelled"] == 3
    events = (await scene.api.get("/api/v1/audit-logs", params={"action": "bot_broadcast_cancelled"})).json()
    assert len(events) == 1 and events[0]["metadata"] == {"cancelled": 3}


async def test_creating_a_broadcast_is_audited_without_the_text_the_people_or_their_telegram_ids(
    scene: Scene, session: AsyncSession
) -> None:
    people = await add_people(session, 2)
    created = await admin_create_broadcast(scene.api, target=TARGET_SELECTED, user_ids=[p.id for p in people])

    events = (await scene.api.get("/api/v1/audit-logs", params={"action": "bot_broadcast_created"})).json()

    assert len(events) == 1 and events[0]["entity_type"] == "bot_broadcast" and events[0]["entity_id"] == created["id"]
    assert events[0]["metadata"]["recipients"] == 2 and events[0]["metadata"]["kind"] == "news"
    dump = await audit_dump()
    assert NEWS_MARKER not in dump
    assert all(str(person.telegram_id) not in dump and str(person.id) not in dump for person in people)


async def test_broadcasts_are_limited_per_admin_with_retry_after(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_people(session, 1)
    limited_at = None
    for attempt in range(1, 30):
        response = await admin_broadcast(api)
        if response.status_code == 429:
            limited_at = attempt
            assert int(response.headers["Retry-After"]) >= 1
            break
        assert response.status_code == 201

    assert limited_at == 11  # 10 рассылок в час на админа (BOT_BROADCASTS_PER_ADMIN)


async def test_another_admin_is_not_limited_by_the_first_ones_broadcasts(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    await add_people(session, 1)
    for _ in range(11):
        await admin_broadcast(api)

    async with login_client(await make_admin(session, AdminRole.OPERATOR)) as operator:
        assert (await admin_broadcast(operator)).status_code == 201


# --- ссылка одному человеку ----------------------------------------------------------------------------------------


async def test_a_link_for_one_person_is_202_and_the_row_is_followed_to_the_end(scene: Scene) -> None:
    user_id = await active_person(scene)

    response = await scene.api.post(user_config_link_url(user_id))

    assert response.status_code == 202, response.text
    queued = response.json()
    assert set(queued) >= {"outbox_id", "status"} and queued["status"] == "pending"
    row = await get_json(scene.api, outbox_url(queued["outbox_id"]))
    assert row["id"] == queued["outbox_id"] and row["status"] == "pending" and row["error"] is None

    claimed = (await http_claim(scene.bot)).json()
    assert (await get_json(scene.api, outbox_url(queued["outbox_id"])))["status"] == "claimed"
    await http_ack(scene.bot, claimed["lease_id"], [ack_result(claimed["items"][0]["id"])])
    assert (await get_json(scene.api, outbox_url(queued["outbox_id"])))["status"] == "sent"


async def test_asking_twice_returns_the_same_open_row(scene: Scene) -> None:
    user_id = await active_person(scene)

    first = await scene.api.post(user_config_link_url(user_id))
    second = await scene.api.post(user_config_link_url(user_id))

    assert second.status_code == 202 and second.json()["outbox_id"] == first.json()["outbox_id"]


async def test_a_failed_send_shows_its_error_code_to_the_admin(scene: Scene) -> None:
    user_id = await active_person(scene)
    queued = (await scene.api.post(user_config_link_url(user_id))).json()
    claimed = (await http_claim(scene.bot)).json()

    await http_ack(
        scene.bot, claimed["lease_id"], [ack_result(claimed["items"][0]["id"], "failed", error="chat_not_found")]
    )

    row = await get_json(scene.api, outbox_url(queued["outbox_id"]))
    assert row["status"] == "failed" and row["error"] == "chat_not_found"


async def test_the_state_of_a_row_shows_neither_the_address_nor_the_telegram_id(scene: Scene) -> None:
    user_id = await active_person(scene)
    queued = (await scene.api.post(user_config_link_url(user_id))).json()
    await http_claim(scene.bot)

    text = (await scene.api.get(outbox_url(queued["outbox_id"]))).text

    assert "/c#" not in text and str(TELEGRAM_ID) not in text


async def test_an_unknown_row_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.get(outbox_url(uuid4()))).status_code == 404


async def test_a_link_for_an_unknown_person_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.post(user_config_link_url(uuid4()))).status_code == 404


async def test_a_link_for_a_person_without_telegram_is_409(api: httpx.AsyncClient, session: AsyncSession) -> None:
    person = await add_person(session, None)

    response = await api.post(user_config_link_url(person.id))

    assert response.status_code == 409 and response.json()["reason"] == NO_TELEGRAM


async def test_a_link_for_a_person_who_blocked_the_bot_is_409(api: httpx.AsyncClient, session: AsyncSession) -> None:
    person = await add_person(session, blocked=True)

    response = await api.post(user_config_link_url(person.id))

    assert response.status_code == 409 and response.json()["reason"] == BOT_BLOCKED


async def test_a_link_for_a_person_without_access_is_409(api: httpx.AsyncClient, session: AsyncSession) -> None:
    person = await add_person(session)

    response = await api.post(user_config_link_url(person.id))

    assert response.status_code == 409 and response.json()["reason"] == NO_ACCESS


async def test_a_link_for_imported_clients_is_409(scene: Scene) -> None:
    user_id = await active_person(scene)
    await sql("UPDATE vpn_credentials SET source = 'IMPORTED', encrypted_config = NULL")

    response = await scene.api.post(user_config_link_url(user_id))

    assert response.status_code == 409 and response.json()["reason"] == CONFIG_UNAVAILABLE


async def test_a_link_without_a_panel_address_is_409(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = await active_person(scene)
    set_public_address(monkeypatch, "", domain="")

    response = await scene.api.post(user_config_link_url(user_id))

    assert response.status_code == 409 and response.json()["reason"] == NOT_CONFIGURED


async def test_the_refusal_of_a_link_queues_nothing_and_issues_nothing(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    person = await add_person(session)

    await api.post(user_config_link_url(person.id))

    assert await sql_rows("SELECT id FROM bot_outbox") == [] and await sql_rows("SELECT id FROM config_links") == []


async def test_queueing_a_link_is_audited_with_the_row_id_only(scene: Scene) -> None:
    user_id = await active_person(scene)
    queued = (await scene.api.post(user_config_link_url(user_id))).json()

    events = (await scene.api.get("/api/v1/audit-logs", params={"action": "bot_config_link_queued"})).json()

    assert len(events) == 1 and events[0]["entity_type"] == "vpn_user" and events[0]["entity_id"] == user_id
    assert events[0]["metadata"] == {"outbox_id": queued["outbox_id"]}
    assert str(TELEGRAM_ID) not in await audit_dump()


async def test_links_are_limited_per_admin_with_retry_after(api: httpx.AsyncClient, session: AsyncSession) -> None:
    people = await add_people(session, 62)
    limited_at = None
    for attempt, person in enumerate(people, start=1):
        response = await api.post(user_config_link_url(person.id))
        if response.status_code == 429:
            limited_at = attempt
            assert int(response.headers["Retry-After"]) >= 1
            break
        assert response.status_code == 409  # доступа нет, но запрос посчитан

    assert limited_at == 61  # 60 отправок за 10 минут на админа (BOT_CONFIG_SENDS_PER_ADMIN)


# --- пользователь в панели: поля Telegram --------------------------------------------------------------------------


async def test_the_user_card_shows_the_telegram_fields(api: httpx.AsyncClient, session: AsyncSession) -> None:
    person = await add_person(session, TELEGRAM_ID, "Ivan Petrov", telegram_username="ivan_tg")
    plain = (await api.post("/api/v1/vpn-users", json={"username": "plain", "email": "plain@example.org"})).json()

    body = await get_json(api, f"/api/v1/vpn-users/{person.id}")
    other = await get_json(api, f"/api/v1/vpn-users/{plain['id']}")

    assert (body["telegram_id"], body["telegram_username"], body["bot_blocked_at"]) == (TELEGRAM_ID, "ivan_tg", None)
    assert (other["telegram_id"], other["telegram_username"], other["bot_blocked_at"]) == (None, None, None)


async def test_the_user_list_shows_the_mark_of_a_person_who_blocked_the_bot(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    await add_person(session, TELEGRAM_ID, "Ivan Petrov", blocked=True)

    (item,) = await get_json(api, "/api/v1/vpn-users")

    assert item["bot_blocked_at"] is not None and item["telegram_id"] == TELEGRAM_ID


# --- права ---------------------------------------------------------------------------------------------------------


async def test_a_viewer_reads_everything_on_the_page_and_writes_nothing(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 1)
    created = await admin_create_broadcast(scene.api)
    person = await add_person(session, SECOND_TELEGRAM_ID)

    async with login_client(await make_admin(session, AdminRole.VIEWER)) as viewer:
        for url in (
            STATUS_URL,
            SETTINGS_URL,
            PROTOCOLS_URL,
            RECIPIENTS_URL,
            "/api/v1/bot-admin/invites",
            "/api/v1/bot-admin/registrations",
            BROADCASTS_URL,
            broadcast_url(created["id"]),
        ):
            assert (await viewer.get(url)).status_code == 200, url
        writes = [
            ("PUT", SETTINGS_URL, {"default_protocol_id": None, "default_days": 30, "invite_valid_days": 7}),
            ("POST", BROADCASTS_URL, broadcast_body()),
            ("POST", AUDIENCE_URL, audience_body()),
            ("POST", broadcast_url(created["id"], "/cancel"), None),
            ("POST", user_config_link_url(person.id), None),
        ]
        for method, url, body in writes:
            assert (await viewer.request(method, url, json=body)).status_code == 403, (method, url)
    assert (await get_json(scene.api, broadcast_url(created["id"])))["state"] == "running"


async def test_an_operator_manages_broadcasts_but_not_keys(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 1)

    async with login_client(await make_admin(session, AdminRole.OPERATOR)) as operator:
        assert (await admin_broadcast(operator)).status_code == 201
        assert (
            await operator.put(
                SETTINGS_URL, json={"default_protocol_id": None, "default_days": 30, "invite_valid_days": 7}
            )
        ).status_code == 200
        assert (await operator.post("/api/v1/bot-admin/keys", json={"name": "x"})).status_code == 403


async def test_without_a_login_every_route_of_the_page_is_401(client: httpx.AsyncClient) -> None:
    for method, url in (
        ("GET", STATUS_URL),
        ("GET", SETTINGS_URL),
        ("PUT", SETTINGS_URL),
        ("GET", PROTOCOLS_URL),
        ("GET", RECIPIENTS_URL),
        ("GET", BROADCASTS_URL),
        ("POST", BROADCASTS_URL),
        ("POST", AUDIENCE_URL),
        ("GET", outbox_url(uuid4())),
        ("POST", user_config_link_url(uuid4())),
    ):
        assert (await client.request(method, url, json={})).status_code == 401, (method, url)


async def test_the_news_text_does_not_get_into_the_logs(
    api: httpx.AsyncClient, session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await add_people(session, 1)
    caplog.set_level(logging.DEBUG)

    await admin_broadcast(api)
    await admin_broadcast(api, text=f"{NEWS_MARKER} <bad>")

    assert NEWS_MARKER not in caplog.text
    assert NEWS_TEXT.split(". ")[0] not in caplog.text
