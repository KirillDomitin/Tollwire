"""A missing file peer never proves its live interface authorization has been removed."""

import pytest

from core.exceptions import AppError
from core.ssh_exception import CommandExecutionError
from infrastructure.vpn.awg_config import list_peers, remove_peer
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT
from infrastructure.vpn.provider import VPNClientStats
from modules.access.models import CredentialState
from modules.access.service import AccessGrantService
from modules.audit.actor import SYSTEM_ACTOR
from modules.servers.service import ServerConnectionService
from modules.servers.sync import ServerSyncService
from tests.fixtures import World
from tests.services.test_gateway_devices import gateway, occupied, target


async def test_revoke_retries_runtime_apply_even_after_file_peer_has_disappeared(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    grant_id = grant.id
    ids = [target(await world.credentials().schedule_issue(grant_id)) for _ in range(5)]
    first = await world.credentials().provision(grant_id, credential_id=ids[0])
    second = await world.credentials().provision(grant_id, credential_id=ids[1])
    first_key, second_key = first.client_public_key, second.client_public_key
    original_apply = AmneziaGatewayProvider._apply_config

    async def fail_apply(self: AmneziaGatewayProvider) -> None:
        raise RuntimeError("syncconf unavailable")

    monkeypatch.setattr(AmneziaGatewayProvider, "_apply_config", fail_apply)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="syncconf unavailable"):
            await world.credentials().deprovision(grant_id, credential_id=ids[0])
        await world.session.rollback()
        assert first_key not in {peer.public_key for peer in list_peers(fake.files[GATEWAY_LAYOUT.host_config_file])}
        assert fake.interface_peers == {first_key, second_key}
        assert len(await occupied(world)) == 5
        with pytest.raises(AppError):
            await world.credentials().schedule_issue(grant_id)
        await world.session.rollback()
    monkeypatch.setattr(AmneziaGatewayProvider, "_apply_config", original_apply)
    await world.credentials().deprovision(grant_id, credential_id=ids[0])
    assert fake.interface_peers == {second_key}
    assert len(await occupied(world)) == 4


async def test_name_only_pending_cleanup_removes_runtime_ghost_when_file_and_key_are_missing(world: World) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    grant_id = grant.id
    credential_id = target(await world.credentials().schedule_issue(grant_id))
    pending = await world.credentials().get(credential_id)
    assert pending.peer_name is not None
    async with await ServerConnectionService(world.session).create_ssh_client(protocol.server_id) as ssh:
        created = await AmneziaGatewayProvider(ssh).create_client(pending.peer_name)
    fake.files[GATEWAY_LAYOUT.host_config_file], _ = remove_peer(
        fake.files[GATEWAY_LAYOUT.host_config_file], created.public_key
    )
    pending.state = CredentialState.PROVISIONING
    await world.session.commit()
    assert pending.client_public_key is None and fake.interface_peers == {created.public_key}
    await world.credentials().deprovision(grant_id, credential_id=credential_id)
    assert fake.interface_peers == set() and await occupied(world) == []


async def test_sync_cleans_stale_runtime_peer_even_when_snapshot_has_no_file_peer(world: World) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    grant_id = grant.id
    credential_id = target(await world.credentials().schedule_issue(grant_id))
    credential = await world.credentials().provision(grant_id, credential_id=credential_id)
    key = credential.client_public_key
    assert key is not None
    fake.files[GATEWAY_LAYOUT.host_config_file], _ = remove_peer(fake.files[GATEWAY_LAYOUT.host_config_file], key)
    await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(grant_id)
    # Legacy/out-of-band expiry can leave an ACTIVE credential on an inactive grant.
    credential.state = CredentialState.ACTIVE
    await world.session.commit()
    assert fake.interface_peers == {key}
    await ServerSyncService(world.session).sync(protocol.server_id)
    assert fake.interface_peers == set() and await occupied(world) == []


@pytest.mark.parametrize("report", ["unavailable", "mismatch"])
async def test_unconfirmed_runtime_snapshot_cannot_release_the_device_slot(
    world: World, monkeypatch: pytest.MonkeyPatch, report: str
) -> None:
    protocol, _ = await gateway(world)
    _, _, grant = await world.access(protocol)
    grant_id = grant.id
    credential_id = target(await world.credentials().schedule_issue(grant_id))
    credential = await world.credentials().provision(grant_id, credential_id=credential_id)
    key = credential.client_public_key
    assert key is not None
    original_stats = AmneziaGatewayProvider.client_stats

    async def unconfirmed(self: AmneziaGatewayProvider) -> list[VPNClientStats] | None:
        return None if report == "unavailable" else [VPNClientStats(key, None, 0, 0)]

    monkeypatch.setattr(AmneziaGatewayProvider, "client_stats", unconfirmed)
    with pytest.raises(CommandExecutionError):
        await world.credentials().deprovision(grant_id, credential_id=credential_id)
    await world.session.rollback()
    assert len(await occupied(world)) == 1
    pending = await world.credentials().get(credential_id)
    assert pending.revoked_at is None and pending.state == CredentialState.REVOKING
    monkeypatch.setattr(AmneziaGatewayProvider, "client_stats", original_stats)
    await world.credentials().deprovision(grant_id, credential_id=credential_id)
    assert await occupied(world) == []
