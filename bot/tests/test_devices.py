"""Device menu uses the caller's identity and stable operation tokens."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from support import TG_ID, World
from tollwire_bot.panel import ConfigLink, Conflict


@pytest.fixture
def devices(world: World, monkeypatch: pytest.MonkeyPatch) -> dict:
    grant = str(uuid4())
    device = str(uuid4())
    data = {
        "limit": 5,
        "occupied": 1,
        "devices": [
            {"id": device, "access_grant_id": grant, "device_name": "Phone", "state": "active", "has_config": True}
        ],
        "gateways": [{"grant_id": grant, "server_name": "Gateway"}],
    }
    monkeypatch.setattr(world.client, "devices", AsyncMock(return_value=data))
    return data


async def test_list_shows_quota_and_uses_targeted_config(world: World, devices: dict, monkeypatch) -> None:
    link = AsyncMock(return_value=ConfigLink(config_url="https://panel.example.test/c#selected", valid_until=None))
    monkeypatch.setattr(world.client, "device_config_link", link)
    person = world.person()
    await person.say("/devices")
    assert "1/5" in person.last and "Phone" in person.last
    button = next(button for button in world.telegram.live_buttons(TG_ID) if button.data.startswith("dev:config:"))
    await person.press_button(button)
    link.assert_awaited_once_with(TG_ID, devices["devices"][0]["id"])
    assert "#selected" in person.last


async def test_replayed_gateway_callback_preserves_request_id(world: World, devices: dict, monkeypatch) -> None:
    add = AsyncMock()
    monkeypatch.setattr(world.client, "add_device", add)
    person = world.person()
    await person.say("/devices")
    await person.press("device_add")
    button = next(button for button in world.telegram.live_buttons(TG_ID) if button.data.startswith("dev:gateway:"))
    await person.press_button(button)
    await person.say("Laptop")
    await person.press_button(button)
    await person.say("Laptop")
    assert add.await_count == 2
    first, second = [call.args for call in add.await_args_list]
    assert first == second
    assert first[:3] == (TG_ID, devices["gateways"][0]["grant_id"], "Laptop")


async def test_revoke_requires_confirmation_and_uses_exact_id(world: World, devices: dict, monkeypatch) -> None:
    revoke = AsyncMock()
    monkeypatch.setattr(world.client, "revoke_device", revoke)
    person = world.person()
    await person.say("/devices")
    button = next(button for button in world.telegram.live_buttons(TG_ID) if button.data.startswith("dev:revoke:"))
    await person.press_button(button)
    revoke.assert_not_awaited()
    await person.press("device_revoke_yes")
    revoke.assert_awaited_once_with(TG_ID, devices["devices"][0]["id"])


async def test_pending_and_full_do_not_offer_config_or_add(world: World, devices: dict) -> None:
    devices["occupied"] = 5
    devices["devices"][0].update(state="provisioning", has_config=False)
    person = world.person()
    await person.say("/devices")
    buttons = world.telegram.live_buttons(TG_ID)
    assert all(not item.data.startswith("dev:config:") and item.data != "dev:add" for item in buttons)
    assert "5/5" in person.last


async def test_stale_quota_reports_backend_limit(world: World, devices: dict, monkeypatch) -> None:
    monkeypatch.setattr(world.client, "add_device", AsyncMock(side_effect=Conflict("device_limit")))
    person = world.person()
    await person.say("/devices")
    await person.press("device_add")
    button = next(button for button in world.telegram.live_buttons(TG_ID) if button.data.startswith("dev:gateway:"))
    await person.press_button(button)
    await person.say("Laptop")
    assert "5 мест" in person.last
