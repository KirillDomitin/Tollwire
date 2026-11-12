"""Device Bot API: ownership, explicit config targets, pending quota and request replay."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest

from core.security import decrypt_secret
from modules.access.models.access_grant import AccessGrant
from modules.access.models.vpn_credential import VPNCredential
from modules.vpn_users.models.user import VPNUser
from tests.bot_support import (
    SECOND_TELEGRAM_ID,
    TELEGRAM_ID,
    admin_create_key,
    audit_dump,
    bot_client,
    links_in_db,
    open_over_http,
    token_of,
)
from tests.fixtures import World
from tests.services.test_gateway_devices import gateway, target


@dataclass
class Scene:
    bot: httpx.AsyncClient
    user: VPNUser
    grant: AccessGrant

    @property
    def url(self) -> str:
        return f"/api/v1/bot/users/by-telegram/{TELEGRAM_ID}/devices"


@pytest.fixture
async def scene(api: httpx.AsyncClient, world: World) -> AsyncIterator[Scene]:
    protocol, _ = await gateway(world)
    user, _, grant = await world.access(protocol)
    user.telegram_id = TELEGRAM_ID
    await world.session.commit()
    key = (await admin_create_key(api))["key"]
    async with bot_client(key) as bot:
        yield Scene(bot, user, grant)


async def reserve(world: World, grant_id: UUID, name: str = "Phone", *, ready: bool = False) -> VPNCredential:
    credential_id = target(await world.credentials().schedule_issue(grant_id, device_name=name))
    if ready:
        return await world.credentials().provision(grant_id, credential_id=credential_id)
    return await world.credentials().get(credential_id)


async def test_bot_device_list_includes_pending_slots_without_secrets(scene: Scene, world: World) -> None:
    ready = await reserve(world, scene.grant.id, ready=True)
    pending = await reserve(world, scene.grant.id, "Laptop")
    response = await scene.bot.get(scene.url)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["occupied"], body["limit"]) == (2, 5)
    assert {item["id"] for item in body["devices"]} == {str(ready.id), str(pending.id)}
    assert {item["grant_id"] for item in body["gateways"]} == {str(scene.grant.id)}
    for item in body["devices"]:
        assert not {"encrypted_config", "config", "encrypted_psk", "peer_name"} & item.keys()
    assert "PrivateKey" not in response.text and ready.encrypted_config not in response.text


@pytest.mark.parametrize("operation", ["revoke", "config-link"])
async def test_bot_cannot_target_another_users_device(
    scene: Scene, world: World, operation: str
) -> None:
    protocol = await world.credentials().protocols.get_by_id(scene.grant.protocol_id)
    assert protocol is not None
    other, _, grant = await world.access(protocol)
    other.telegram_id = SECOND_TELEGRAM_ID
    await world.session.commit()
    device = await reserve(world, grant.id, ready=True)
    response = await scene.bot.post(f"{scene.url}/{device.id}/{operation}")
    assert response.status_code == 404, response.text
    await world.session.refresh(device)
    assert device.revoked_at is None
    assert await links_in_db() == []


async def test_bot_cannot_add_to_another_users_gateway_grant(scene: Scene, world: World) -> None:
    protocol = await world.credentials().protocols.get_by_id(scene.grant.protocol_id)
    assert protocol is not None
    _, _, foreign_grant = await world.access(protocol)
    response = await scene.bot.post(scene.url, json={
        "grant_id": str(foreign_grant.id), "device_name": " чужой", "request_id": str(uuid4()),
    })
    assert response.status_code == 404, response.text
    assert (await scene.bot.get(scene.url)).json()["occupied"] == 0


async def test_selected_device_link_opens_its_config_and_reissue_never_consumes_a_slot(
    scene: Scene, world: World
) -> None:
    first = await reserve(world, scene.grant.id, "Phone", ready=True)
    second = await reserve(world, scene.grant.id, "Laptop", ready=True)
    response = await scene.bot.post(f"{scene.url}/{first.id}/config-link")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    token = token_of(response.json()["config_url"])
    opened = await open_over_http(scene.bot, token)
    assert opened.status_code == 200, opened.text
    assert opened.json()["config"] == decrypt_secret(first.encrypted_config)
    assert opened.json()["config"] != decrypt_secret(second.encrypted_config)
    repeated = await scene.bot.post(f"{scene.url}/{first.id}/config-link")
    assert repeated.status_code == 200
    assert (await scene.bot.get(scene.url)).json()["occupied"] == 2
    assert token not in await audit_dump()


async def test_pending_selected_device_link_returns_not_ready_without_falling_back(scene: Scene, world: World) -> None:
    await reserve(world, scene.grant.id, "Ready", ready=True)
    pending = await reserve(world, scene.grant.id, "Pending")
    response = await scene.bot.post(f"{scene.url}/{pending.id}/config-link")
    assert response.status_code == 409 and response.json()["reason"] == "not_ready", response.text
    assert await links_in_db() == []


async def test_panel_and_bot_share_reservations_and_callback_replay(scene: Scene, api: httpx.AsyncClient) -> None:
    payload = {"grant_id": str(scene.grant.id), "device_name": "Phone", "request_id": str(uuid4())}
    first = await scene.bot.post(scene.url, json=payload)
    repeated = await scene.bot.post(scene.url, json=payload)
    assert first.status_code == repeated.status_code == 202
    assert first.json()["id"] == repeated.json()["id"]
    for index in range(4):
        response = await api.post(f"/api/v1/vpn-users/{scene.user.id}/gateway-devices", json={
            **payload, "device_name": f"Panel {index}", "request_id": str(uuid4()),
        })
        assert response.status_code == 202, response.text
    sixth = await scene.bot.post(scene.url, json={**payload, "request_id": str(uuid4())})
    assert sixth.status_code == 409 and sixth.json()["reason"] == "device_limit", sixth.text
    assert (await scene.bot.get(scene.url)).json()["occupied"] == 5


@pytest.mark.parametrize("field,value", [("grant_id", "bad"), ("request_id", "bad"), ("device_name", "")])
async def test_invalid_device_input_is_rejected(scene: Scene, field: str, value: str) -> None:
    body = {"grant_id": str(scene.grant.id), "request_id": str(uuid4()), "device_name": "Phone", field: value}
    response = await scene.bot.post(scene.url, json=body)
    assert response.status_code == 422


async def test_device_routes_require_bot_key(scene: Scene) -> None:
    async with bot_client() as anonymous:
        assert (await anonymous.get(scene.url)).status_code == 401
        response = await anonymous.post(scene.url, json={
            "grant_id": str(scene.grant.id), "request_id": str(uuid4()),
        })
        assert response.status_code == 401
