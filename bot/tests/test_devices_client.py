"""Device API contracts on the real HTTP client, including untrusted response validation."""

from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from fakes import FakeClock, FakePanel, dropped
from support import TG_ID, make_client
from tollwire_bot.panel import Conflict, PanelUnexpected

LIST = "GET /users/by-telegram/*/devices"
ADD = "POST /users/by-telegram/*/devices"
LINK = "POST /users/by-telegram/*/devices/*/config-link"


@asynccontextmanager
async def opened(panel: FakePanel):
    client = make_client(panel, panel.clock)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def panel() -> FakePanel:
    result = FakePanel(FakeClock())
    result.add_gateway(TG_ID)
    result.add_device(TG_ID)
    return result


async def test_all_device_methods_use_exact_identity_targets_and_bot_key(panel: FakePanel) -> None:
    grant = panel.device_gateways[TG_ID][0]["grant_id"]
    device = panel.device_rows[TG_ID][0]["id"]
    request_id = str(uuid4())
    async with opened(panel) as client:
        listed = await client.devices(TG_ID)
        await client.add_device(TG_ID, grant, "Laptop", request_id)
        link = await client.device_config_link(TG_ID, device)
        await client.revoke_device(TG_ID, device)
    assert listed is not None and listed["occupied"] == 1
    assert link is not None and link.config_url == panel.links[-1].url
    assert all(f"/users/by-telegram/{TG_ID}/devices" in call.path for call in panel.calls)
    assert all(call.headers["x-bot-key"] == panel.key for call in panel.calls)
    assert panel.bodies(ADD) == [{"grant_id": grant, "device_name": "Laptop", "request_id": request_id}]


@pytest.mark.parametrize("method", ["add", "revoke"])
async def test_unregistered_device_mutations_map_404_to_no_access(panel: FakePanel, method: str) -> None:
    panel.users.clear()
    async with opened(panel) as client:
        with pytest.raises(Conflict) as error:
            if method == "add":
                await client.add_device(TG_ID, str(uuid4()), "Phone", str(uuid4()))
            else:
                await client.revoke_device(TG_ID, str(uuid4()))
    assert error.value.reason == "no_access"


async def test_unregistered_device_list_and_target_link_return_none(panel: FakePanel) -> None:
    panel.users.clear()
    async with opened(panel) as client:
        assert await client.devices(TG_ID) is None
        assert await client.device_config_link(TG_ID, str(uuid4())) is None


async def test_client_network_retry_reuses_the_identical_request_uuid(panel: FakePanel) -> None:
    grant = panel.device_gateways[TG_ID][0]["grant_id"]
    panel.override(ADD, dropped())

    async def instant(_: float) -> None:
        return None

    client = make_client(panel, panel.clock)
    client._sleep = instant
    try:
        await client.add_device(TG_ID, grant, "Laptop", str(uuid4()))
    finally:
        await client.aclose()
    assert len(panel.bodies(ADD)) == 2 and panel.bodies(ADD)[0] == panel.bodies(ADD)[1]
    assert len(panel.device_rows[TG_ID]) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("occupied", True),
        ("occupied", -1),
        ("limit", True),
        ("limit", 0),
        ("limit", 6),
        ("devices", [None]),
        ("gateways", [None]),
    ],
)
async def test_invalid_device_collection_is_safe_panelunexpected(panel: FakePanel, field: str, value: object) -> None:
    response = panel._dispatch(LIST, f"/api/v1/bot/users/by-telegram/{TG_ID}/devices", None).json()
    response[field] = value
    panel.override(LIST, lambda: httpx.Response(200, json=response))
    async with opened(panel) as client:
        with pytest.raises(PanelUnexpected):
            await client.devices(TG_ID)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "not-a-uuid"),
        ("state", "secret-server-traceback"),
        ("has_config", "true"),
        ("device_name", {"secret": "private"}),
        ("access_grant_id", "bad"),
    ],
)
async def test_invalid_device_item_cannot_escape_client_validation(panel: FakePanel, field: str, value: object) -> None:
    response = deepcopy(panel._dispatch(LIST, f"/api/v1/bot/users/by-telegram/{TG_ID}/devices", None).json())
    response["devices"][0][field] = value
    panel.override(LIST, lambda: httpx.Response(200, json=response))
    async with opened(panel) as client:
        with pytest.raises(PanelUnexpected) as error:
            await client.devices(TG_ID)
    assert "secret-server-traceback" not in str(error.value) and "private" not in str(error.value)


async def test_target_link_response_without_url_fails_without_echoing_body(panel: FakePanel) -> None:
    panel.override(LINK, lambda: httpx.Response(200, json={"config": "PrivateKey = TOP-SECRET"}))
    async with opened(panel) as client:
        with pytest.raises(PanelUnexpected) as error:
            await client.device_config_link(TG_ID, panel.device_rows[TG_ID][0]["id"])
    assert "TOP-SECRET" not in str(error.value)
