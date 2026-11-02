"""Удаление отозванных записей и атомарный массовый выпуск приглашений."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from core.security import hash_token
from modules.bot.models.invite import BotInvite
from modules.bot.repository import BotInviteRepository
from tests.bot_support import (
    INVITES_URL,
    admin_create_invite,
    admin_create_key,
    audit_dump,
    bot_client,
    register,
    sql,
    sql_rows,
    tables_dump,
)
from tests.fixtures import World

BATCH_URL = f"{INVITES_URL}/batch"


async def batch_body(world: World, count: Any = 3) -> dict[str, Any]:
    _, protocol, _ = await world.server()
    assert protocol is not None
    return {
        "protocol_id": str(protocol.id),
        "days": 14,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "note": "пакет для группы",
        "count": count,
    }


@pytest.mark.parametrize("kind", ["keys", "invites"])
async def test_only_revoked_records_can_be_deleted(api: httpx.AsyncClient, world: World, kind: str) -> None:
    if kind == "keys":
        created = await admin_create_key(api)
    else:
        body = await batch_body(world)
        created = await admin_create_invite(api, body["protocol_id"])
    url = f"/api/v1/bot-admin/{kind}/{created['id']}"

    response = await api.delete(url)

    assert response.status_code == 409, response.text
    assert any(item["id"] == created["id"] for item in (await api.get(url.rsplit("/", 1)[0])).json())


@pytest.mark.parametrize("kind,action", [("keys", "bot_key_deleted"), ("invites", "bot_invite_deleted")])
async def test_delete_revoked_record_once_and_keep_secret_free_audit(
    api: httpx.AsyncClient, world: World, kind: str, action: str
) -> None:
    if kind == "keys":
        created = await admin_create_key(api)
        secret = created["key"]
    else:
        body = await batch_body(world)
        created = await admin_create_invite(api, body["protocol_id"])
        secret = created["code"]
    url = f"/api/v1/bot-admin/{kind}/{created['id']}"
    assert (await api.post(f"{url}/revoke")).status_code == 200

    response = await api.delete(url)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert all(item["id"] != created["id"] for item in (await api.get(url.rsplit("/", 1)[0])).json())
    assert (await api.delete(url)).status_code == 404
    audit = await audit_dump()
    assert audit.count(action) == 1
    assert secret not in audit and hash_token(secret) not in audit
    if kind == "keys":
        async with bot_client(secret) as bot:
            assert (await bot.post("/api/v1/bot/heartbeat", json={})).status_code == 401


@pytest.mark.parametrize("clear_used_at", [False, True], ids=["used", "registration-without-used-at"])
async def test_delete_never_cascades_into_a_registration(
    api: httpx.AsyncClient, world: World, clear_used_at: bool
) -> None:
    body = await batch_body(world)
    invite = await admin_create_invite(api, body["protocol_id"])
    registration = await register(world.session, invite["code"])
    registration_id = registration.id
    await sql("UPDATE bot_invites SET revoked_at = now() WHERE id = :id", id=UUID(invite["id"]))
    if clear_used_at:
        await sql("UPDATE bot_invites SET used_at = NULL WHERE id = :id", id=UUID(invite["id"]))
    before = await tables_dump("bot_registrations", "vpn_users", "access_grants", "subscriptions")

    response = await api.delete(f"{INVITES_URL}/{invite['id']}")

    assert response.status_code == 409, response.text
    assert await tables_dump("bot_registrations", "vpn_users", "access_grants", "subscriptions") == before
    assert await sql_rows("SELECT id FROM bot_registrations WHERE id = :id", id=registration_id)


@pytest.mark.parametrize("kind", ["keys", "invites"])
async def test_delete_unknown_record_is_not_found(api: httpx.AsyncClient, kind: str) -> None:
    assert (await api.delete(f"/api/v1/bot-admin/{kind}/{uuid4()}")).status_code == 404


@pytest.mark.parametrize("count", [1, 100])
async def test_batch_creates_unique_invites_without_storing_or_logging_codes(
    api: httpx.AsyncClient, world: World, count: int, caplog: pytest.LogCaptureFixture
) -> None:
    body = await batch_body(world, count)
    with caplog.at_level(logging.INFO):
        response = await api.post(BATCH_URL, json=body)

    assert response.status_code == 201, response.text
    assert "no-store" in response.headers.get("cache-control", "")
    items = response.json()["items"]
    assert len(items) == count
    assert len({item["code"] for item in items}) == count
    assert len({item["id"] for item in items}) == count
    persisted = await sql_rows("SELECT id, code_hash, protocol_id, days, note FROM bot_invites")
    assert len(persisted) == count
    assert {row[1] for row in persisted} == {hash_token(item["code"]) for item in items}
    assert all(str(row[2]) == body["protocol_id"] and row[3:] == (14, body["note"]) for row in persisted)
    hidden = "\n".join(
        [await audit_dump(), await tables_dump("bot_invites"), (await api.get(INVITES_URL)).text, caplog.text]
    )
    for item in items:
        assert item["code"] not in hidden
        assert item["link"] is None  # без heartbeat нельзя придумывать имя бота
    audit = await audit_dump()
    assert audit.count("bot_invite_created") == count
    assert all(item["id"] in audit for item in items)


@pytest.mark.parametrize("count", [0, 101, 1.5])
async def test_batch_rejects_invalid_count_without_side_effects(
    api: httpx.AsyncClient, world: World, count: Any
) -> None:
    body = await batch_body(world, count)
    before = await audit_dump()

    response = await api.post(BATCH_URL, json=body)

    assert response.status_code == 422, response.text
    assert await sql_rows("SELECT id FROM bot_invites") == []
    assert await audit_dump() == before


async def test_batch_rolls_back_invites_and_audit_after_a_mid_batch_failure(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = await batch_body(world)
    original = BotInviteRepository.create
    calls = 0

    async def fail_third(self: BotInviteRepository, invite: BotInvite) -> BotInvite:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("synthetic batch insert failure")
        return await original(self, invite)

    monkeypatch.setattr(BotInviteRepository, "create", fail_third)
    before = await audit_dump()
    try:
        response = await api.post(BATCH_URL, json=body)
    except RuntimeError as error:
        assert str(error) == "synthetic batch insert failure"
    else:
        assert response.status_code == 500, response.text

    assert calls == 3
    assert await sql_rows("SELECT id FROM bot_invites") == []
    assert await audit_dump() == before
