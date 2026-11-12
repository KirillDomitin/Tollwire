"""Gateway device reservations: a user's five slots survive retries and uncertain SSH outcomes."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from core.database.postgres import session_factory
from core.exceptions import AppError
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT
from infrastructure.vpn.provider import VPNClientConfig
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import VPNCredentialRepository
from modules.access.schemas import AccessGrantCreate
from modules.access.service import AccessGrantService, SubscriptionService, VPNCredentialService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import Job, JobType
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.servers.service import ServerConnectionService
from modules.servers.sync import ServerSyncService
from tests.fakes.fake_ssh import FakeServer
from tests.fixtures import World


async def gateway(world: World) -> tuple[VPNProtocol, FakeServer]:
    server, _, _ = await world.server(install=False)
    fake = world.fakes[server.id] = FakeServer(host=server.host, layout=GATEWAY_LAYOUT)
    protocol = await ServerConnectionService(world.session).protocol_install(
        VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_GATEWAY)
    )
    return protocol, fake


def target(job: Job) -> UUID:
    assert job.type == JobType.CREATE_DEVICE
    assert job.params is not None
    return UUID(job.params["credential_id"])


async def occupied(world: World) -> list[VPNCredential]:
    return list(
        (await world.session.scalars(select(VPNCredential).where(VPNCredential.revoked_at.is_(None)))).all()
    )


async def test_pending_devices_reserve_all_five_slots_across_gateways(world: World) -> None:
    first, first_fake = await gateway(world)
    second, second_fake = await gateway(world)
    _, subscription, first_grant = await world.access(first)
    second_grant = await AccessGrantService(world.session, SYSTEM_ACTOR).create(
        subscription.id, AccessGrantCreate(protocol_id=second.id)
    )
    jobs = [
        await world.credentials().schedule_issue(
            (first_grant if index % 2 else second_grant).id, device_name=f"Device {index}"
        )
        for index in range(5)
    ]
    assert len({target(job) for job in jobs}) == 5
    assert len(await occupied(world)) == 5
    assert not first_fake.interface_peers and not second_fake.interface_peers
    with pytest.raises(AppError):
        await world.credentials().schedule_issue(second_grant.id, device_name="Sixth")
    await world.session.rollback()
    assert len(await occupied(world)) == 5


async def test_concurrent_last_slot_is_serialized_on_user_across_gateways(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = await gateway(world)
    second, _ = await gateway(world)
    _, subscription, first_grant = await world.access(first)
    second_grant = await AccessGrantService(world.session, SYSTEM_ACTOR).create(
        subscription.id, AccessGrantCreate(protocol_id=second.id)
    )
    for _ in range(4):
        await world.credentials().schedule_issue(first_grant.id)
    first_counting, release_first, second_locking = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_count = VPNCredentialRepository.count_gateway_for_user
    original_lock = VPNCredentialRepository.lock_user
    async with session_factory() as panel_session, session_factory() as bot_session:

        async def pause_first_count(self: VPNCredentialRepository, user_id: UUID) -> int:
            if self.session is panel_session:
                first_counting.set()
                await release_first.wait()
            return await original_count(self, user_id)

        async def observe_second_lock(self: VPNCredentialRepository, user_id: UUID) -> None:
            if self.session is bot_session:
                second_locking.set()
            await original_lock(self, user_id)

        monkeypatch.setattr(VPNCredentialRepository, "count_gateway_for_user", pause_first_count)
        monkeypatch.setattr(VPNCredentialRepository, "lock_user", observe_second_lock)
        panel = asyncio.create_task(
            VPNCredentialService(panel_session, SYSTEM_ACTOR).schedule_issue(first_grant.id, device_name="Panel")
        )
        try:
            await asyncio.wait_for(first_counting.wait(), timeout=5)
            bot = asyncio.create_task(
                VPNCredentialService(bot_session, SYSTEM_ACTOR).schedule_issue(second_grant.id, device_name="Bot")
            )
            await asyncio.wait_for(second_locking.wait(), timeout=5)
        finally:
            release_first.set()
        results = await asyncio.wait_for(asyncio.gather(panel, bot, return_exceptions=True), timeout=10)
        assert sum(isinstance(result, Job) for result in results) == 1
        assert sum(isinstance(result, AppError) for result in results) == 1
    assert len(await occupied(world)) == 5


async def test_saved_device_retry_preserves_keys_and_other_devices(world: World) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    one = target(await world.credentials().schedule_issue(grant.id, device_name="Phone"))
    two = target(await world.credentials().schedule_issue(grant.id, device_name="Laptop"))
    first = await world.credentials().provision(grant.id, credential_id=one)
    second = await world.credentials().provision(grant.id, credential_id=two)
    original = (first.client_public_key, first.encrypted_config)
    again = await world.credentials().provision(grant.id, credential_id=one)
    assert again.id == one and (again.client_public_key, again.encrypted_config) == original
    assert first.client_public_key != second.client_public_key
    assert fake.interface_peers == {first.client_public_key, second.client_public_key}
    assert len(await world.audit(AuditAction.CREDENTIAL_ISSUED, one)) == 1


async def test_repeated_add_request_reserves_only_one_device(world: World) -> None:
    protocol, _ = await gateway(world)
    _, _, grant = await world.access(protocol)
    request_id = uuid4()
    first = await world.credentials().schedule_issue(grant.id, device_name="Phone", request_id=request_id)
    again = await world.credentials().schedule_issue(grant.id, device_name="Phone", request_id=request_id)
    assert target(first) == target(again)
    assert len(await occupied(world)) == 1


async def test_individual_revoke_keeps_other_device_and_frees_only_one_slot(world: World) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    ids = [target(await world.credentials().schedule_issue(grant.id)) for _ in range(5)]
    first = await world.credentials().provision(grant.id, credential_id=ids[0])
    second = await world.credentials().provision(grant.id, credential_id=ids[1])
    job = await world.credentials().schedule_revoke(first.id)
    assert isinstance(job, Job) and job.type == JobType.REVOKE_DEVICE
    assert job.params is not None and job.params["credential_id"] == str(first.id)
    with pytest.raises(AppError):
        await world.credentials().schedule_issue(grant.id)
    await world.session.rollback()
    await world.credentials().deprovision(grant.id, credential_id=first.id)
    assert second.client_public_key in fake.interface_peers
    assert first.client_public_key not in fake.interface_peers
    assert len(await occupied(world)) == 4
    replacement = target(await world.credentials().schedule_issue(grant.id))
    assert replacement not in ids
    assert len(await occupied(world)) == 5


async def test_subscription_revoke_includes_ready_and_reserved_devices(world: World) -> None:
    protocol, fake = await gateway(world)
    _, subscription, grant = await world.access(protocol)
    ids = [target(await world.credentials().schedule_issue(grant.id)) for _ in range(3)]
    await world.credentials().provision(grant.id, credential_id=ids[0])
    await SubscriptionService(world.session, SYSTEM_ACTOR).revoke(subscription.id)
    for credential_id in ids:
        await world.credentials().deprovision(grant.id, credential_id=credential_id)
    assert not fake.interface_peers and not await occupied(world)
    with pytest.raises(AppError):
        await world.credentials().provision(grant.id, credential_id=ids[1])
    assert not fake.interface_peers


async def test_lost_ssh_result_keeps_reservation_and_retry_cleans_old_peer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    ids = [target(await world.credentials().schedule_issue(grant.id)) for _ in range(5)]
    original = AmneziaGatewayProvider.create_client

    async def create_then_lose_response(self: AmneziaGatewayProvider, name: str) -> VPNClientConfig:
        await original(self, name)
        raise TimeoutError("lost SSH response")

    monkeypatch.setattr(AmneziaGatewayProvider, "create_client", create_then_lose_response)
    with pytest.raises(TimeoutError, match="lost SSH response"):
        await world.credentials().provision(grant.id, credential_id=ids[0])
    await world.session.rollback()
    assert len(fake.interface_peers) == 1 and len(await occupied(world)) == 5
    with pytest.raises(AppError):
        await world.credentials().schedule_issue(grant.id)
    await world.session.rollback()
    monkeypatch.setattr(AmneziaGatewayProvider, "create_client", original)
    ready = await world.credentials().provision(grant.id, credential_id=ids[0])
    assert ready.id == ids[0] and fake.interface_peers == {ready.client_public_key}


async def test_failed_cleanup_does_not_free_slot_or_create_another_peer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    ids = [target(await world.credentials().schedule_issue(grant.id)) for _ in range(5)]
    ready = await world.credentials().provision(grant.id, credential_id=ids[0])

    async def cannot_confirm_cleanup(self: AmneziaGatewayProvider, public_key: str) -> None:
        raise TimeoutError("cleanup not confirmed")

    monkeypatch.setattr(AmneziaGatewayProvider, "revoke_client", cannot_confirm_cleanup)
    await world.credentials().schedule_revoke(ready.id)
    with pytest.raises(TimeoutError, match="cleanup not confirmed"):
        await world.credentials().deprovision(grant.id, credential_id=ready.id)
    await world.session.rollback()
    assert len(await occupied(world)) == 5 and fake.interface_peers == {ready.client_public_key}
    with pytest.raises(AppError):
        await world.credentials().schedule_issue(grant.id)


async def test_sync_restores_two_devices_without_replacing_existing_config(world: World) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    ids = [target(await world.credentials().schedule_issue(grant.id)) for _ in range(2)]
    devices = [await world.credentials().provision(grant.id, credential_id=credential_id) for credential_id in ids]
    originals = [(device.client_public_key, device.encrypted_config) for device in devices]
    assert originals[0][0] != originals[1][0]
    async with await ServerConnectionService(world.session).create_ssh_client(protocol.server_id) as ssh:
        provider = AmneziaGatewayProvider(ssh)
        for device in devices:
            await provider.revoke_client(device.client_public_key)
        await ServerSyncService(world.session)._reconcile_clients(provider, protocol, [], [])
    assert fake.interface_peers == {device.client_public_key for device in devices}
    assert [(device.client_public_key, device.encrypted_config) for device in devices] == originals


async def test_direct_awg_keeps_single_client_policy(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    _, _, grant = await world.access(protocol)
    first = await world.credentials().schedule_issue(grant.id)
    assert first.type == JobType.CREATE_USER
    with pytest.raises(AppError):
        await world.credentials().schedule_issue(grant.id)
