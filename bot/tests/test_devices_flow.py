"""Device handlers through the dispatcher, real PanelClient and stateful Bot API fake."""

import io

import httpx
import pytest

from fakes import Button, conflict
from support import OTHER_TG_ID, TG_ID, Person, World, setup_logging, t

LIST = "GET /users/by-telegram/*/devices"
ADD = "POST /users/by-telegram/*/devices"
LINK = "POST /users/by-telegram/*/devices/*/config-link"
REVOKE = "POST /users/by-telegram/*/devices/*/revoke"


@pytest.fixture
def devices(world: World) -> dict:
    gateway = world.panel.add_gateway(TG_ID)
    device = world.panel.add_device(TG_ID)
    return {"gateway": gateway, "device": device}


def button(world: World, prefix: str) -> Button:
    return next(item for item in reversed(world.telegram.live_buttons(TG_ID)) if item.data.startswith(prefix))


async def begin_add(world: World, person: Person) -> Button:
    await person.say("/devices")
    await person.press("device_add")
    choice = button(world, "dev:gateway:")
    await person.press_button(choice)
    return choice


async def test_list_and_config_address_the_selected_device(world: World, devices: dict) -> None:
    other = world.panel.add_device(TG_ID, "Laptop")
    person = world.person()
    await person.say("/devices")
    assert "2/5" in person.last and "Phone" in person.last and "Laptop" in person.last
    await person.press_button(button(world, f"dev:config:{devices['device']['id']}"))
    (call,) = [call for call in world.panel.calls if call.route == LINK]
    assert call.path.endswith(f"/{TG_ID}/devices/{devices['device']['id']}/config-link")
    assert world.panel.links[-1].credential_id == devices["device"]["id"] != other["id"]
    assert world.panel.links[-1].url in person.last
    assert len(world.panel.device_rows[TG_ID]) == 2


async def test_multiple_gateways_require_explicit_choice(world: World, devices: dict) -> None:
    second = world.panel.add_gateway(TG_ID, "Gateway two")
    person = world.person()
    await person.say("/devices")
    await person.press("device_add")
    choices = [item for item in world.telegram.live_buttons(TG_ID) if item.data.startswith("dev:gateway:")]
    assert {item.text for item in choices} == {"Gateway", "Gateway two"}
    assert world.panel.count(ADD) == 0
    await person.press_button(next(item for item in choices if item.data.endswith(second["grant_id"])))
    await person.say("Tablet")
    (body,) = world.panel.bodies(ADD)
    assert body["grant_id"] == second["grant_id"] and body["device_name"] == "Tablet"


async def test_lost_reply_and_replayed_gateway_callback_keep_same_uuid(world: World, devices: dict) -> None:
    person = world.person()
    choice = await begin_add(world, person)

    def commit_then_disconnect() -> Exception:
        call = [item for item in world.panel.calls if item.route == ADD][-1]
        assert world.panel._dispatch(call.route, call.path, call.body).status_code == 202
        return httpx.ReadTimeout("reply lost")

    world.panel.override(ADD, commit_then_disconnect)
    await person.say("Laptop")
    await person.press_button(choice)
    await person.say("Laptop")
    bodies = world.panel.bodies(ADD)
    assert len(bodies) == 3 and bodies[0] == bodies[1] == bodies[2]
    assert len(world.panel.device_rows[TG_ID]) == 2


async def test_pending_full_quota_offers_no_config_or_add(world: World, devices: dict) -> None:
    devices["device"].update(state="provisioning", has_config=False)
    for index in range(4):
        world.panel.add_device(TG_ID, f"Pending {index}", "reserved")
    person = world.person()
    await person.say("/devices")
    assert "5/5" in person.last
    assert all(
        not item.data.startswith("dev:config:") and item.data != "dev:add"
        for item in world.telegram.live_buttons(TG_ID)
    )


async def test_backend_quota_error_after_selection_is_explained(world: World, devices: dict) -> None:
    person = world.person()
    await begin_add(world, person)
    world.panel.override(ADD, conflict("device_limit"))
    await person.say("Laptop")
    assert person.last == t("device_limit") and len(world.panel.device_rows[TG_ID]) == 1


async def test_pending_revoke_requires_confirmation_and_retains_slot(world: World, devices: dict) -> None:
    devices["device"].update(state="reserved", has_config=False)
    person = world.person()
    await person.say("/devices")
    await person.press_button(button(world, "dev:revoke:"))
    assert world.panel.count(REVOKE) == 0
    await person.press("device_revoke_yes")
    assert world.panel.count(REVOKE) == 1
    assert devices["device"]["state"] == "revoking" and "1/5" in person.last


@pytest.mark.parametrize("action", ["config", "remove"])
@pytest.mark.parametrize("removed_user", [False, True])
async def test_foreign_or_removed_user_callback_returns_safe_text(
    world: World, devices: dict, action: str, removed_user: bool
) -> None:
    world.panel.add_gateway(OTHER_TG_ID)
    foreign = world.panel.add_device(OTHER_TG_ID, "Secret device")
    person = world.person()
    await person.say("/devices")
    shown = button(world, "dev:list")
    if removed_user:
        del world.panel.users[TG_ID]
    await person.press_button(Button(shown.message_id, "Forged", f"dev:{action}:{foreign['id']}"))
    assert person.last in (t("config_no_access"), t("not_registered"))
    assert foreign["state"] == "active" and world.panel.links == []


@pytest.mark.parametrize(
    "reason,key",
    [
        ("not_ready", "config_not_ready"),
        ("config_unavailable", "config_unavailable"),
    ],
)
async def test_stale_config_callback_explains_pending_or_missing_config(
    world: World, devices: dict, reason: str, key: str
) -> None:
    person = world.person()
    await person.say("/devices")
    selected = button(world, "dev:config:")
    world.panel.override(LINK, conflict(reason))
    await person.press_button(selected)
    assert person.last == t(key) and world.panel.links == []


async def test_mixed_protocol_user_keeps_legacy_config_and_selects_gateway_via_devices(
    world: World, devices: dict
) -> None:
    world.panel.add_device(TG_ID, "Laptop")
    world.panel.users[TG_ID]["protocol"] = "amnezia_wg"
    person = world.person()
    await person.say("/config")
    assert len(world.panel.links) == 1
    assert world.panel.links[0].credential_id is None  # legacy/direct credential remains accessible
    assert world.panel.links[0].url in person.last
    assert world.panel.count(LINK) == 0
    await person.say("/devices")
    assert "Phone" in person.last and "Laptop" in person.last
    assert len([item for item in world.telegram.live_buttons(TG_ID) if item.data.startswith("dev:config:")]) == 2
    await person.press_button(button(world, f"dev:config:{devices['device']['id']}"))
    assert world.panel.links[-1].credential_id == devices["device"]["id"]
    assert world.panel.links[0].live  # targeted gateway link does not invalidate the direct config link


async def test_inactive_grant_does_not_offer_config_even_if_device_active(world: World, devices: dict) -> None:
    world.panel.device_gateways[TG_ID] = []
    await world.person().say("/devices")
    assert not any(item.data.startswith("dev:config:") for item in world.telegram.live_buttons(TG_ID))


async def test_start_command_escapes_add_device_name_state(world: World, devices: dict) -> None:
    person = world.person()
    await begin_add(world, person)
    await person.say("/start")
    assert world.panel.count(ADD) == 0


async def test_labels_and_config_urls_do_not_appear_in_logs(world: World, devices: dict) -> None:
    stream = io.StringIO()
    setup_logging(world.settings, stream)
    person = world.person()
    await begin_add(world, person)
    await person.say("Private family laptop")
    await person.press_button(button(world, f"dev:config:{devices['device']['id']}"))
    logged = stream.getvalue()
    assert "Private family laptop" not in logged and world.panel.key not in logged
    assert world.panel.links and all(link.url not in logged and link.token not in logged for link in world.panel.links)


async def test_group_devices_command_never_contacts_panel(world: World, devices: dict) -> None:
    before = world.panel.count(LIST)
    await world.person(chat_type="group").say("/devices")
    assert world.panel.count(LIST) == before
