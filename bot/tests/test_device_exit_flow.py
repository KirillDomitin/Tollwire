"""Exit selection preserves a single explicit, versioned user intention."""

from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from support import TG_ID, World
from tollwire_bot.panel import Conflict, PanelUnavailable


@pytest.fixture
def selection(world: World, monkeypatch: pytest.MonkeyPatch) -> dict:
    credential, grant, old, new = [str(uuid4()) for _ in range(4)]
    dto = {
        "credential_id": credential,
        "device_name": "Phone",
        "current_exit": {"id": old, "name": "Old"},
        "pinned_exit": None,
        "mode": "automatic",
        "revision": str(uuid4()),
        "exits": [{"id": new, "name": "New", "selectable": True, "reason": None}],
    }
    monkeypatch.setattr(
        world.client,
        "devices",
        AsyncMock(
            return_value={
                "limit": 5,
                "occupied": 1,
                "gateways": [{"grant_id": grant, "server_name": "Gateway"}],
                "devices": [
                    {
                        "id": credential,
                        "access_grant_id": grant,
                        "device_name": "Phone",
                        "state": "active",
                        "has_config": True,
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(world.client, "device_exit_options", AsyncMock(return_value=dto), raising=False)

    async def applied(telegram_id, credential_id, exit_id, revision):
        result = deepcopy(dto)
        target = next((item for item in dto["exits"] if item["id"] == exit_id), None)
        result["pinned_exit"] = None if target is None else {"id": target["id"], "name": target["name"]}
        result["mode"] = "automatic" if target is None else "pinned"
        if target is not None:
            result["current_exit"] = result["pinned_exit"]
        return result

    monkeypatch.setattr(world.client, "change_device_exit", AsyncMock(side_effect=applied), raising=False)
    return dto


def button(world: World, action: str):
    return next(b for b in reversed(world.telegram.live_buttons(TG_ID)) if action in b.data)


async def open_selection(world: World) -> None:
    person = world.person()
    await person.say("/devices")
    await person.press_button(button(world, "dev:exit:"))


async def test_selection_requires_confirmation_and_duplicate_is_safe(world: World, selection: dict) -> None:
    await open_selection(world)
    assert "Phone" in world.person().last and "Old" in world.person().last
    assert all(len(b.data.encode()) <= 64 for b in world.telegram.live_buttons(TG_ID))
    await world.person().press_button(button(world, ":pick:0"))
    assert "Old" in world.person().last and "New" in world.person().last
    world.client.change_device_exit.assert_not_awaited()
    confirm = button(world, ":confirm")
    await world.person().press_button(confirm)
    await world.person().press_button(confirm)
    world.client.change_device_exit.assert_awaited_once_with(
        TG_ID, selection["credential_id"], selection["exits"][0]["id"], selection["revision"]
    )


async def test_cancel_and_expired_confirmation_do_not_write(world: World, selection: dict) -> None:
    await open_selection(world)
    await world.person().press_button(button(world, ":cancel"))
    world.client.change_device_exit.assert_not_awaited()
    await open_selection(world)
    await world.person().press_button(button(world, ":pick:0"))
    confirm = button(world, ":confirm")
    world.clock.advance(601)
    await world.person().press_button(confirm)
    world.client.change_device_exit.assert_not_awaited()


@pytest.mark.parametrize("failure", [Conflict("stale_confirmation"), PanelUnavailable("timeout")])
async def test_ambiguous_response_reads_state_without_new_version_retry(
    world: World, selection: dict, failure: Exception
) -> None:
    await open_selection(world)
    await world.person().press_button(button(world, ":pick:0"))
    confirm = button(world, ":confirm")
    world.client.change_device_exit.side_effect = failure
    await world.person().press_button(confirm)
    await world.person().press_button(confirm)
    assert world.client.change_device_exit.await_count == 1
    assert world.client.device_exit_options.await_count >= 2


async def test_unpin_uses_null_and_original_revision(world: World, selection: dict) -> None:
    selection["pinned_exit"] = selection["current_exit"]
    selection["mode"] = "pinned"
    await open_selection(world)
    await world.person().press_button(button(world, ":auto"))
    await world.person().press_button(button(world, ":confirm"))
    world.client.change_device_exit.assert_awaited_once_with(
        TG_ID, selection["credential_id"], None, selection["revision"]
    )


async def test_foreign_user_message_and_group_cannot_confirm(world: World, selection: dict) -> None:
    await open_selection(world)
    await world.person().press_button(button(world, ":pick:0"))
    confirm = button(world, ":confirm")
    for change in ("user", "message", "group"):
        update = world.updates.callback(TG_ID, confirm, "confirm")
        raw = update["callback_query"]
        if change == "user":
            raw["from"]["id"] = TG_ID + 1
        elif change == "message":
            raw["message"]["message_id"] += 100
        else:
            raw["message"]["chat"]["type"] = "group"
        await world.dp.feed_raw_update(world.bot, update)
    world.client.change_device_exit.assert_not_awaited()


async def test_restart_preserves_valid_confirmation(world: World, selection: dict, monkeypatch) -> None:
    await open_selection(world)
    await world.person().press_button(button(world, ":pick:0"))
    confirm = button(world, ":confirm")
    await world.restart()
    mutate = AsyncMock(return_value=selection)
    monkeypatch.setattr(world.client, "change_device_exit", mutate)
    await world.person().press_button(confirm)
    mutate.assert_awaited_once_with(
        TG_ID, selection["credential_id"], selection["exits"][0]["id"], selection["revision"]
    )


async def test_consumed_confirmation_after_restart_only_reads(world: World, selection: dict, monkeypatch) -> None:
    await open_selection(world)
    await world.person().press_button(button(world, ":pick:0"))
    confirm = button(world, ":confirm")
    key = world.person().storage_key()
    saved = await world.storage.get_data(key)
    saved["exit_operation"]["phase"] = "consumed"
    await world.storage.set_data(key, saved)
    await world.restart()
    mutate, read = AsyncMock(), AsyncMock(return_value=selection)
    monkeypatch.setattr(world.client, "change_device_exit", mutate)
    monkeypatch.setattr(world.client, "device_exit_options", read)
    await world.person().press_button(confirm)
    mutate.assert_not_awaited()
    read.assert_awaited_once_with(TG_ID, selection["credential_id"])


@pytest.mark.parametrize("reason", ["exit_unavailable", "exit_full", "gateway_busy", "no_access", "not_ready"])
async def test_refused_change_offers_bound_refresh(world: World, selection: dict, reason: str) -> None:
    await open_selection(world)
    await world.person().press_button(button(world, ":pick:0"))
    world.client.change_device_exit.side_effect = Conflict(reason)
    await world.person().press_button(button(world, ":confirm"))
    await world.person().press_button(button(world, ":refresh"))
    assert world.client.device_exit_options.await_count == 2
    assert world.client.change_device_exit.await_count == 1


async def test_empty_options_still_allow_refresh_and_cancel(world: World, selection: dict) -> None:
    selection["exits"] = []
    await open_selection(world)
    assert "нет" in world.person().last
    assert not any(":pick:" in b.data for b in world.telegram.live_buttons(TG_ID))
    await world.person().press_button(button(world, ":refresh"))
    await world.person().press_button(button(world, ":cancel"))
    world.client.change_device_exit.assert_not_awaited()


async def test_valid_but_unapplied_response_is_read_without_success(world: World, selection: dict) -> None:
    from tollwire_bot.texts import t

    await open_selection(world)
    await world.person().press_button(button(world, ":pick:0"))
    world.client.change_device_exit.side_effect = None
    world.client.change_device_exit.return_value = selection
    await world.person().press_button(button(world, ":confirm"))
    assert t("device_exit_applied") not in world.person().texts
    assert world.client.device_exit_options.await_count == 2


async def test_new_devices_menu_invalidates_confirmation(world: World, selection: dict) -> None:
    await open_selection(world)
    await world.person().press_button(button(world, ":pick:0"))
    confirm = button(world, ":confirm")
    await world.person().say("/devices")
    await world.person().press_button(confirm)
    world.client.change_device_exit.assert_not_awaited()
