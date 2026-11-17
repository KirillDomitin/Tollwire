"""Self-service exit switching: public Bot API, CAS, capacity and failure safety."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, update

from core.database.postgres import session_factory
from core.exceptions import SSHAuthError, SSHConnectionError, SSHTimeoutError
from core.ssh_exception import CommandExecutionError
from infrastructure.workers.worker import JobWorker
from modules.access.models import CredentialState
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.gateway.assignment import GatewayAssignmentService
from modules.gateway.failover import GatewayFailoverService
from modules.gateway.models.gateway_exit import ExitState, GatewayExit
from tests.bot_support import TELEGRAM_ID, admin_create_key, bot_client
from tests.fixtures import World
from tests.integration.test_gateway_assignments_api import assignments, gateway_with_exits
from tests.integration.test_gateway_devices_bot_api import reserve
from tests.integration.test_gateway_exits_api import add_exit, exit_server, exits_url, scene
from tests.integration.test_gateway_failover_api import fresh, patch_url, stale, tick


@dataclass
class SwitchScene:
    bot: httpx.AsyncClient
    url: str
    sid: UUID
    exits: list[dict[str, Any]]
    credential: Any
    subscription_id: UUID


@pytest.fixture
async def switch(api: httpx.AsyncClient, worker: JobWorker, world: World) -> AsyncIterator[SwitchScene]:
    gw, protocol, exits = await gateway_with_exits(api, worker, world)
    row = await world.credentials().protocols.get_by_id(UUID(protocol["id"]))
    assert row is not None
    user, subscription, grant = await world.access(row)
    user.telegram_id = TELEGRAM_ID
    await world.session.commit()
    credential = await reserve(world, grant.id, ready=True)
    key = (await admin_create_key(api))["key"]
    async with bot_client(key) as bot:
        yield SwitchScene(
            bot,
            f"/api/v1/bot/users/by-telegram/{TELEGRAM_ID}/devices/{credential.id}",
            gw.sid,
            exits,
            credential,
            subscription.id,
        )


async def options(switch: SwitchScene) -> dict[str, Any]:
    response = await switch.bot.get(f"{switch.url}/exit-options")
    assert response.status_code == 200, response.text
    return response.json()


async def choose(switch: SwitchScene, target: str | None, revision: str | None) -> httpx.Response:
    return await switch.bot.patch(f"{switch.url}/exit", json={"exit_id": target, "expected_revision": revision})


async def test_options_expose_only_safe_device_and_exit_data(switch: SwitchScene) -> None:
    body = await options(switch)
    assert body["credential_id"] == str(switch.credential.id)
    assert UUID(body["revision"])
    assert {row["id"] for row in body["exits"]} == {row["id"] for row in switch.exits}
    assert all(set(row) == {"id", "name", "selectable", "reason"} for row in body["exits"])
    assert not {"config", "encrypted_config", "client_address", "client_public_key", "host"} & body.keys()
    assert "PrivateKey" not in str(body) and switch.credential.encrypted_config not in str(body)


async def test_switch_pins_one_device_and_replay_is_stale(switch: SwitchScene, api: httpx.AsyncClient) -> None:
    before = await options(switch)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])
    response = await choose(switch, target, before["revision"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_exit"]["id"] == body["pinned_exit"]["id"] == target
    assert body["mode"] == "pinned" and body["revision"] != before["revision"]
    replay = await choose(switch, target, before["revision"])
    assert replay.status_code == 409 and replay.json()["reason"] == "stale_confirmation"
    assert (await options(switch))["revision"] == body["revision"]


async def test_unpin_preserves_actual_exit_and_invalidates_aba_confirmation(switch: SwitchScene) -> None:
    original = await options(switch)
    target = original["current_exit"]["id"]
    pinned = await choose(switch, target, original["revision"])
    assert pinned.status_code == 200, pinned.text
    unpinned = await choose(switch, None, pinned.json()["revision"])
    assert unpinned.status_code == 200, unpinned.text
    assert unpinned.json()["current_exit"]["id"] == target
    assert unpinned.json()["pinned_exit"] is None and unpinned.json()["mode"] == "automatic"
    repinned = await choose(switch, target, unpinned.json()["revision"])
    assert repinned.status_code == 200
    stale = await choose(switch, None, pinned.json()["revision"])
    assert stale.status_code == 409 and stale.json()["reason"] == "stale_confirmation"


async def test_current_exit_noop_does_not_consume_capacity_or_change_revision(
    switch: SwitchScene, api: httpx.AsyncClient
) -> None:
    before = await options(switch)
    target = before["current_exit"]["id"]
    pinned = await choose(switch, target, before["revision"])
    assert pinned.status_code == 200, pinned.text
    limit = await api.patch(f"/api/v1/servers/{switch.sid}/gateway/exits/{target}", json={"max_clients": 1})
    assert limit.status_code == 200, limit.text
    repeated = await choose(switch, target, pinned.json()["revision"])
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["revision"] == pinned.json()["revision"]


@pytest.mark.parametrize("method,tail", [("GET", "exit-options"), ("PATCH", "exit")])
async def test_switch_routes_require_bot_key_and_hide_foreign_device(
    switch: SwitchScene, method: str, tail: str
) -> None:
    kwargs = {"json": {"exit_id": None, "expected_revision": None}} if method == "PATCH" else {}
    async with bot_client() as anonymous:
        assert (await anonymous.request(method, f"{switch.url}/{tail}", **kwargs)).status_code == 401
    foreign_url = switch.url.replace(str(TELEGRAM_ID), str(TELEGRAM_ID + 1))
    assert (await switch.bot.request(method, f"{foreign_url}/{tail}", **kwargs)).status_code == 404


@pytest.mark.parametrize("method,tail", [("GET", "exit-options"), ("PATCH", "exit")])
async def test_owner_cannot_substitute_another_users_existing_credential(
    switch: SwitchScene, world: World, api: httpx.AsyncClient, method: str, tail: str
) -> None:
    grant = await world.credentials().grants.get_by_id(switch.credential.access_grant_id)
    assert grant is not None
    protocol = await world.credentials().protocols.get_by_id(grant.protocol_id)
    assert protocol is not None
    _, _, foreign_grant = await world.access(protocol)
    foreign = await reserve(world, foreign_grant.id, ready=True)
    routing = await assignments(api, switch.sid)
    url = switch.url.replace(str(switch.credential.id), str(foreign.id))
    kwargs = {"json": {"exit_id": None, "expected_revision": None}} if method == "PATCH" else {}
    response = await switch.bot.request(method, f"{url}/{tail}", **kwargs)
    assert response.status_code == 404, response.text
    assert await assignments(api, switch.sid) == routing


@pytest.mark.parametrize(
    "payload",
    [{"exit_id": "bad", "expected_revision": None}, {"exit_id": None, "expected_revision": "bad"}, {"exit_id": None}],
)
async def test_switch_input_is_validated(switch: SwitchScene, payload: dict[str, Any]) -> None:
    assert (await switch.bot.patch(f"{switch.url}/exit", json=payload)).status_code == 422


@pytest.mark.parametrize("state", [ExitState.DRAINING, ExitState.REMOVING])
async def test_exit_removed_since_options_is_rejected(switch: SwitchScene, state: ExitState) -> None:
    before = await options(switch)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])
    async with session_factory() as session:
        await session.execute(update(GatewayExit).where(GatewayExit.id == UUID(target)).values(state=state))
        await session.commit()
    response = await choose(switch, target, before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "exit_unavailable"
    assert (await options(switch))["current_exit"] == before["current_exit"]


async def test_expired_access_is_checked_again_at_confirmation(switch: SwitchScene) -> None:
    before = await options(switch)
    async with session_factory() as session:
        await session.execute(
            update(Subscription)
            .where(Subscription.id == switch.subscription_id)
            .values(valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await session.commit()
    response = await choose(switch, before["current_exit"]["id"], before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "no_access"


async def test_foreign_exit_id_is_safe_unavailable(switch: SwitchScene) -> None:
    before = await options(switch)
    response = await choose(switch, str(uuid4()), before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "exit_unavailable"


async def test_existing_exit_of_another_gateway_cannot_be_selected(
    switch: SwitchScene, api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    foreign = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.99")
    target = await add_exit(api, worker, foreign.sid, server.id)
    before = await options(switch)
    assert target["id"] not in {row["id"] for row in before["exits"]}
    response = await choose(switch, target["id"], before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "exit_unavailable", response.text
    assert await options(switch) == before


@pytest.mark.parametrize("state", [CredentialState.PROVISIONING, CredentialState.REVOKING, CredentialState.REVOKED])
@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_ineligible_device_cannot_read_or_change_exit(
    switch: SwitchScene, state: CredentialState, method: str
) -> None:
    from modules.gateway.models.gateway_assignment import GatewayAssignment

    before = await options(switch)
    async with session_factory() as session:
        routing = (await session.execute(select(GatewayAssignment.__table__))).all()
        await session.execute(
            update(VPNCredential)
            .where(VPNCredential.id == switch.credential.id)
            .values(state=state, revoked_at=datetime.now(timezone.utc) if state == CredentialState.REVOKED else None)
        )
        await session.commit()
    response = (
        await switch.bot.get(f"{switch.url}/exit-options")
        if method == "GET"
        else await choose(switch, before["current_exit"]["id"], before["revision"])
    )
    assert response.status_code == 409 and response.json()["reason"] == "not_ready", response.text
    # The admin list hides revoked credentials; inspect persisted routing instead.
    async with session_factory() as session:
        assert (await session.execute(select(GatewayAssignment.__table__))).all() == routing


async def test_tunnel_failed_after_options_is_rejected_without_changing_route(
    switch: SwitchScene, world: World
) -> None:
    before = await options(switch)
    target = next(row for row in switch.exits if row["id"] != before["current_exit"]["id"])
    assert next(row for row in before["exits"] if row["id"] == target["id"])["selectable"]
    world.fakes[switch.sid].exit_handshakes[target["slot"]] = stale()
    response = await choose(switch, target["id"], before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "exit_unavailable", response.text
    after = await options(switch)
    assert (after["current_exit"], after["pinned_exit"], after["revision"]) == (
        before["current_exit"],
        before["pinned_exit"],
        before["revision"],
    )


@pytest.mark.parametrize("error_type", [CommandExecutionError, SSHConnectionError, SSHTimeoutError, SSHAuthError])
async def test_apply_failure_rolls_back_assignment_pin_and_revision(
    switch: SwitchScene, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    before = await options(switch)
    assignments_before = await assignments(api, switch.sid)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise error_type("private-secret /opt/vpn-admin/gw/assign.rules")

    monkeypatch.setattr(GatewayAssignmentService, "_apply_map", broken)
    response = await choose(switch, target, before["revision"])
    assert response.status_code == 502, response.text
    assert "private-secret" not in response.text and "assign.rules" not in response.text
    assert await assignments(api, switch.sid) == assignments_before
    assert (await options(switch))["revision"] == before["revision"]


async def test_admin_pin_changes_invalidate_bot_confirmation(switch: SwitchScene, api: httpx.AsyncClient) -> None:
    before = await options(switch)
    response = await api.patch(patch_url(switch.sid, str(switch.credential.id)), json={"pin": True})
    assert response.status_code == 200, response.text
    stale = await choose(switch, None, before["revision"])
    assert stale.status_code == 409 and stale.json()["reason"] == "stale_confirmation"


async def test_two_devices_cannot_take_the_same_last_place(
    switch: SwitchScene, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await options(switch)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])
    # Put both clients on the source using the unchanged administrative contract.
    other = await reserve(world, switch.credential.access_grant_id, "Laptop", ready=True)
    from modules.audit.actor import SYSTEM_ACTOR

    await GatewayFailoverService(world.session, SYSTEM_ACTOR).move_client(
        switch.sid, other.id, UUID(before["current_exit"]["id"]), False
    )
    async with session_factory() as session:
        await session.execute(update(GatewayExit).where(GatewayExit.id == UUID(target)).values(max_clients=1))
        await session.commit()
    other_url = switch.url.replace(str(switch.credential.id), str(other.id))
    other_options = await switch.bot.get(f"{other_url}/exit-options")
    assert other_options.status_code == 200, other_options.text
    original = GatewayAssignmentService._ready
    contenders = []

    async def competing_confirmation(self: Any, gateway: Any, provider: Any) -> None:
        await original(self, gateway, provider)
        # The first request holds ServerLock while the second tries the last slot.
        contenders.append(
            await switch.bot.patch(
                f"{other_url}/exit", json={"exit_id": target, "expected_revision": other_options.json()["revision"]}
            )
        )

    monkeypatch.setattr(GatewayAssignmentService, "_ready", competing_confirmation)
    first = await choose(switch, target, before["revision"])
    assert first.status_code == 200, first.text
    assert len(contenders) == 1
    assert contenders[0].status_code == 409 and contenders[0].json()["reason"] == "gateway_busy"
    monkeypatch.setattr(GatewayAssignmentService, "_ready", original)
    second = await switch.bot.patch(
        f"{other_url}/exit", json={"exit_id": target, "expected_revision": other_options.json()["revision"]}
    )
    assert second.status_code == 409 and second.json()["reason"] == "exit_full", second.text


async def test_access_revoked_during_ready_is_rechecked_after_its_commit(
    switch: SwitchScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await options(switch)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])
    original = GatewayAssignmentService._ready

    async def expire_after_ready(self: Any, gateway: Any, provider: Any) -> None:
        await original(self, gateway, provider)
        await self.session.commit()  # _ready is allowed to commit after container recreation.
        async with session_factory() as session:
            await session.execute(
                update(Subscription)
                .where(Subscription.id == switch.subscription_id)
                .values(valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
            await session.commit()

    monkeypatch.setattr(GatewayAssignmentService, "_ready", expire_after_ready)
    response = await choose(switch, target, before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "no_access", response.text


async def test_revoke_requested_during_ready_prevents_device_move(
    switch: SwitchScene, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await options(switch)
    routing = await assignments(api, switch.sid)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])
    original = GatewayAssignmentService._ready

    async def revoke_after_ready(self: Any, gateway: Any, provider: Any) -> None:
        await original(self, gateway, provider)
        await self.session.commit()
        revoked = await switch.bot.post(f"{switch.url}/revoke")
        assert revoked.status_code == 200 and revoked.json() == {"status": "accepted"}, revoked.text

    monkeypatch.setattr(GatewayAssignmentService, "_ready", revoke_after_ready)
    response = await choose(switch, target, before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "not_ready", response.text
    assert await assignments(api, switch.sid) == routing


async def test_exit_removal_requested_during_ready_prevents_move(
    switch: SwitchScene, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await options(switch)
    routing = await assignments(api, switch.sid)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])
    original = GatewayAssignmentService._ready

    async def remove_after_ready(self: Any, gateway: Any, provider: Any) -> None:
        await original(self, gateway, provider)
        await self.session.commit()
        removed = await api.delete(exits_url(switch.sid, target))
        assert removed.status_code == 202, removed.text

    monkeypatch.setattr(GatewayAssignmentService, "_ready", remove_after_ready)
    response = await choose(switch, target, before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "exit_unavailable", response.text
    assert await assignments(api, switch.sid) == routing


async def test_failover_between_authorization_and_lock_invalidates_confirmation(
    switch: SwitchScene, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await options(switch)
    source = next(row for row in switch.exits if row["id"] == before["current_exit"]["id"])
    target = next(row["id"] for row in switch.exits if row["id"] != source["id"])
    original = GatewayFailoverService._move_owned

    async def failover_before_lock(self: Any, *args: Any, **kwargs: Any) -> Any:
        world.fakes[switch.sid].exit_handshakes[source["slot"]] = stale()
        await tick(worker)
        await tick(worker)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(GatewayFailoverService, "_move_owned", failover_before_lock)
    response = await choose(switch, source["id"], before["revision"])
    assert response.status_code == 409 and response.json()["reason"] == "stale_confirmation", response.text
    after = await options(switch)
    assert after["current_exit"]["id"] == target and after["pinned_exit"] is None
    assert after["revision"] != before["revision"]


async def test_bot_pin_survives_failover_and_returns_after_recovery(
    switch: SwitchScene, worker: JobWorker, world: World
) -> None:
    before = await options(switch)
    home = next(row for row in switch.exits if row["id"] == before["current_exit"]["id"])
    pinned = await choose(switch, home["id"], before["revision"])
    assert pinned.status_code == 200, pinned.text
    gateway = world.fakes[switch.sid]
    gateway.exit_handshakes[home["slot"]] = stale()
    await tick(worker)
    await tick(worker)
    away = await options(switch)
    assert away["current_exit"]["id"] != home["id"] and away["pinned_exit"]["id"] == home["id"]
    assert away["revision"] != pinned.json()["revision"]
    gateway.exit_handshakes[home["slot"]] = fresh()
    await tick(worker)
    assert (await options(switch))["current_exit"] == away["current_exit"]
    async with session_factory() as session:
        await session.execute(
            update(GatewayExit)
            .where(GatewayExit.id == UUID(home["id"]))
            .values(healthy_since=datetime.now(timezone.utc) - timedelta(seconds=601))
        )
        await session.commit()
    await tick(worker)
    returned = await options(switch)
    assert returned["current_exit"]["id"] == returned["pinned_exit"]["id"] == home["id"]
    assert returned["revision"] != away["revision"]


async def test_switch_preserves_other_devices_keys_and_quota(switch: SwitchScene, world: World) -> None:
    devices = [switch.credential]
    for index in range(4):
        devices.append(await reserve(world, switch.credential.access_grant_id, f"Device {index}", ready=True))
    identities = {row.id: (row.client_public_key, row.client_address, row.encrypted_config) for row in devices}
    from modules.gateway.models.gateway_assignment import GatewayAssignment

    async with session_factory() as session:
        other_assignments = {row.id: (await session.get(GatewayAssignment, row.id)).exit_id for row in devices[1:]}
    before = await options(switch)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])
    response = await choose(switch, target, before["revision"])
    assert response.status_code == 200, response.text
    for row in devices:
        await world.session.refresh(row)
        assert (row.client_public_key, row.client_address, row.encrypted_config) == identities[row.id]
    async with session_factory() as session:
        for device_id, exit_id in other_assignments.items():
            assignment = await session.get(GatewayAssignment, device_id)
            assert assignment is not None and assignment.exit_id == exit_id
    listed = await switch.bot.get(switch.url.rsplit("/", 1)[0])
    assert listed.status_code == 200 and listed.json()["occupied"] == 5


async def test_user_move_is_audited_without_config_or_address(switch: SwitchScene) -> None:
    from sqlalchemy import select

    from modules.audit.models.audit_log import AuditAction, AuditLog

    before = await options(switch)
    target = next(row["id"] for row in switch.exits if row["id"] != before["current_exit"]["id"])
    response = await choose(switch, target, before["revision"])
    assert response.status_code == 200, response.text
    async with session_factory() as session:
        events = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.GATEWAY_CLIENT_MOVED,
                    AuditLog.entity_id == switch.credential.id,
                )
            )
        ).all()
        assert events
        dump = str([event.meta for event in events])
        assert "bot" in dump and str(switch.credential.id) in dump
        assert switch.credential.encrypted_config not in dump
        assert switch.credential.client_address not in dump
        assert "PrivateKey" not in dump
