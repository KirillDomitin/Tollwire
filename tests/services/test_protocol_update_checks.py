"""Cached update availability: real DB/Redis, fake SSH and registry metadata, deterministic clock."""

import importlib
import importlib.util
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest

from infrastructure.vpn import provider
from infrastructure.vpn.amnezia import AmneziaWGProvider
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from tests.fakes.protocol_update import DIGEST, INDEX_IMAGE, NEW_IMAGE, OLD_IMAGE
from tests.fixtures import World

NOW = datetime(2026, 11, 4, 0, 5, tzinfo=timezone.utc)


def module() -> Any:
    name = "modules.servers.protocol_updates"
    assert importlib.util.find_spec(name) is not None, "Cached protocol update check service is required"
    return importlib.import_module(name)


class Metadata:
    def __init__(self, image_id: str = OLD_IMAGE) -> None:
        self.image_id = image_id
        self.calls = 0
        self.offline = False

    async def latest_for(self, image: Any) -> Any:
        self.calls += 1
        if self.offline:
            response = httpx.Response(429, request=httpx.Request("GET", "https://registry-1.docker.io/v2/"))
            raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)
        registry = importlib.import_module("infrastructure.vpn.registry")
        return registry.RegistryImageInfo(image_id=self.image_id, digest=DIGEST)


def observation(result: dict[str, Any], protocol_id: UUID) -> dict[str, Any]:
    return next(item for item in result["protocols"] if str(item["protocol_id"]) == str(protocol_id))


async def scene(world: World, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    checks = module()
    image_cls = getattr(provider, "ProviderImageInfo", None)
    assert image_cls is not None
    server, protocol, fake = await world.server()

    async def info(self: Any) -> Any:
        return image_cls(image_id=OLD_IMAGE, os="linux", architecture="amd64")

    monkeypatch.setattr(AmneziaWGProvider, "image_info", info, raising=False)
    return checks, (server, protocol, fake), Metadata()


@pytest.mark.parametrize("target,status", [(OLD_IMAGE, "current"), (NEW_IMAGE, "available")])
async def test_check_compares_config_image_id_and_does_not_modify_vpn_or_health(
    world: World, monkeypatch: pytest.MonkeyPatch, target: str, status: str
) -> None:
    checks, (server, protocol, fake), metadata = await scene(world, monkeypatch)
    metadata.image_id = target
    before, commands, health = dict(fake.files), list(fake.commands), server.health_status
    result = await checks.ProtocolUpdateCheckService(world.session, registry=metadata, clock=lambda: NOW).check(
        server.id
    )
    item = observation(result, protocol.id)
    assert item["status"] == status and item["stale"] is False
    assert (item["current_image_id"], item["target_image_id"]) == (OLD_IMAGE, target)
    assert item["checked_at"] is not None and item["last_attempt_at"] is not None
    assert fake.files == before and fake.commands == commands
    await world.session.refresh(server)
    assert server.health_status == health


@pytest.mark.parametrize("target,status", [(OLD_IMAGE, "current"), (NEW_IMAGE, "available")])
async def test_containerd_check_compares_canonical_config_but_preserves_raw_container_image_id(
    world: World, monkeypatch: pytest.MonkeyPatch, target: str, status: str
) -> None:
    checks, (server, protocol, fake), _ = await scene(world, monkeypatch)
    registry = importlib.import_module("infrastructure.vpn.registry")
    assert "current_image_id" in registry.RegistryImageInfo.__dataclass_fields__, (
        "resolved installed config is required"
    )

    async def info(self: Any) -> Any:
        return provider.ProviderImageInfo(image_id=INDEX_IMAGE, os="linux", architecture="amd64")

    class ContainerdMetadata:
        async def latest_for(self, image: Any) -> Any:
            assert image.image_id == INDEX_IMAGE
            return registry.RegistryImageInfo(image_id=target, digest=DIGEST, current_image_id=OLD_IMAGE)

    monkeypatch.setattr(AmneziaWGProvider, "image_info", info)
    before, commands = dict(fake.files), list(fake.commands)
    service = checks.ProtocolUpdateCheckService(world.session, registry=ContainerdMetadata(), clock=lambda: NOW)
    item = observation(await service.check(server.id), protocol.id)
    assert item["status"] == status and item["stale"] is False
    assert (item["current_image_id"], item["target_image_id"]) == (INDEX_IMAGE, target)
    assert observation(await service.get_cached(server.id), protocol.id) == item
    assert fake.files == before and fake.commands == commands and fake.running


async def test_empty_cache_is_unknown_and_cached_read_never_contacts_registry_or_ssh(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks, (server, protocol, fake), metadata = await scene(world, monkeypatch)
    commands = list(fake.commands)
    item = observation(
        await checks.ProtocolUpdateCheckService(world.session, registry=metadata, clock=lambda: NOW).get_cached(
            server.id
        ),
        protocol.id,
    )
    assert item["status"] == "unknown" and item["checked_at"] is None
    assert metadata.calls == 0 and fake.commands == commands


async def test_success_becomes_stale_after_26_hours_and_expires_after_seven_days(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks, (server, protocol, _), metadata = await scene(world, monkeypatch)
    moment = [NOW]
    service = checks.ProtocolUpdateCheckService(world.session, registry=metadata, clock=lambda: moment[0])
    await service.check(server.id)
    moment[0] = NOW + timedelta(hours=25, minutes=59)
    assert observation(await service.get_cached(server.id), protocol.id)["stale"] is False
    moment[0] = NOW + timedelta(hours=26, seconds=1)
    aged = observation(await service.get_cached(server.id), protocol.id)
    assert aged["status"] == "current" and aged["stale"] is True and aged["checked_at"] is not None
    moment[0] = NOW + timedelta(days=7, seconds=1)
    expired = observation(await service.get_cached(server.id), protocol.id)
    assert expired["status"] == "unknown" and expired["checked_at"] is None


async def test_failed_latest_attempt_is_unknown_stale_and_keeps_last_success_timestamp(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks, (server, protocol, _), metadata = await scene(world, monkeypatch)
    moment = [NOW]
    service = checks.ProtocolUpdateCheckService(world.session, registry=metadata, clock=lambda: moment[0])
    successful = observation(await service.check(server.id), protocol.id)
    metadata.offline = True
    moment[0] += timedelta(minutes=5)
    failed = observation(await service.check(server.id), protocol.id)
    assert failed["status"] == "unknown" and failed["stale"] is True
    assert failed["checked_at"] == successful["checked_at"]
    assert failed["last_attempt_at"] != successful["last_attempt_at"]
    assert failed["reason"] is not None and "https://" not in failed["reason"]


async def test_external_app_is_unsupported_and_deleted_protocol_cache_is_not_returned(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks, (server, protocol, _), metadata = await scene(world, monkeypatch)
    app = VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_APP_AWG2, port=51821)
    world.session.add(app)
    await world.session.commit()
    service = checks.ProtocolUpdateCheckService(world.session, registry=metadata, clock=lambda: NOW)
    result = await service.check(server.id)
    assert observation(result, app.id)["status"] == "unsupported"
    assert metadata.calls == 1  # app never reaches SSH/registry.
    removed_id = protocol.id
    await world.session.delete(protocol)
    await world.session.commit()
    cached = await service.get_cached(server.id)
    assert str(removed_id) not in {str(item["protocol_id"]) for item in cached["protocols"]}


@pytest.mark.parametrize("read_only", [False, True], ids=["explicit-check", "cached-read"])
async def test_running_container_update_never_publishes_current_or_available(
    world: World, monkeypatch: pytest.MonkeyPatch, read_only: bool
) -> None:
    checks, (server, protocol, _), metadata = await scene(world, monkeypatch)
    metadata.image_id = NEW_IMAGE
    moment = [NOW]
    service = checks.ProtocolUpdateCheckService(world.session, registry=metadata, clock=lambda: moment[0])
    successful = observation(await service.check(server.id), protocol.id)
    assert successful["status"] == "available"
    kind = getattr(JobType, "UPDATE_PROTOCOL", None)
    assert kind is not None
    world.session.add(
        Job(type=kind, status=JobStatus.RUNNING, server_id=server.id, params={"protocol_type": "amnezia_wg"})
    )
    await world.session.commit()
    moment[0] += timedelta(minutes=1)
    result = await (service.get_cached(server.id) if read_only else service.check(server.id))
    item = observation(result, protocol.id)
    assert item["status"] == "unknown" and item["stale"] is True
    assert item["reason"] == "update_in_progress" and item["checked_at"] == successful["checked_at"]
    assert metadata.calls == 1


@pytest.mark.parametrize("target,status", [(OLD_IMAGE, "current"), (NEW_IMAGE, "available")])
async def test_update_finishing_between_generation_check_and_save_cannot_resurrect_old_observation(
    world: World, monkeypatch: pytest.MonkeyPatch, target: str, status: str
) -> None:
    from core.redis import get_redis

    checks, (server, protocol, _), metadata = await scene(world, monkeypatch)
    metadata.image_id = target
    service = checks.ProtocolUpdateCheckService(world.session, registry=metadata, clock=lambda: NOW)
    original_save = service._save
    publishing: list[str] = []

    async def save_after_update(item: dict[str, Any], generation: str | None) -> None:
        publishing.append(item["status"])
        # The earlier busy/generation comparison has already accepted this observation.
        # Simulate an entire container update starting and finishing before the save resumes:
        # no active marker or RUNNING job remains, but its persistent generation changed.
        await get_redis().incr(checks.generation_key(server.id))
        await original_save(item, generation)

    monkeypatch.setattr(service, "_save", save_after_update)
    await service.check(server.id)
    assert publishing == [status], "the race must occur after the initial comparison, not before it"
    cached = observation(await service.get_cached(server.id), protocol.id)
    assert cached["status"] == "unknown" and cached["stale"] is True
    assert cached["reason"] == "update_in_progress"
