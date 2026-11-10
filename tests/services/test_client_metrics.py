"""Runtime snapshots stay isolated by server/protocol and expose client-side directions."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from core.exceptions import NotFoundError
from core.redis import get_redis
from modules.monitoring.client_metrics import ClientMetricsService, cache_key
from modules.monitoring.service import MetricsCollector
from tests.fixtures import World


async def test_collector_exposes_only_known_clients_with_client_side_directions(world: World) -> None:
    server, protocol, fake = await world.server()
    assert protocol is not None
    _, _, _, credential = await world.issued(protocol)
    fake.transfers[credential.client_public_key] = (123, 456)
    fake.handshakes[credential.client_public_key] = 1_700_000_000
    fake.interface_peers.add("runtime-peer-not-managed-by-panel")
    assert await MetricsCollector().collect(server.id)

    result = await ClientMetricsService(world.session).get_cached(server.id)
    (sample,) = result.protocols
    (client,) = sample.clients
    assert sample.protocol_id == protocol.id and sample.sampled_at is not None and not sample.stale
    assert client.credential_id == credential.id
    assert (client.downloaded_bytes, client.uploaded_bytes) == (456, 123)
    assert client.latest_handshake == datetime.fromtimestamp(1_700_000_000, timezone.utc)
    assert "public_key" not in result.model_dump_json()
    assert "private" not in result.model_dump_json().lower()
    assert 0 < await get_redis().ttl(cache_key(protocol.id)) <= 86400


async def test_cached_reads_never_connect_and_do_not_mix_servers(world: World) -> None:
    server, protocol, fake = await world.server()
    other, other_protocol, _ = await world.server()
    assert protocol is not None and other_protocol is not None
    _, _, _, credential = await world.issued(protocol)
    _, _, _, other_credential = await world.issued(other_protocol)
    await get_redis().set(
        cache_key(protocol.id),
        json.dumps(
            {
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "peers": [
                    {
                        "public_key": other_credential.client_public_key,
                        "latest_handshake": None,
                        "rx_bytes": 900,
                        "tx_bytes": 800,
                    }
                ],
            }
        ),
    )
    before = list(fake.commands)
    result = await ClientMetricsService(world.session).get_cached(server.id)
    assert result.protocols[0].clients == []
    assert fake.commands == before
    assert credential.id != other_credential.id and server.id != other.id


@pytest.mark.parametrize("age,stale", [(0, False), (301, True)])
async def test_cached_sample_age_and_nullable_counters_are_explicit(world: World, age: int, stale: bool) -> None:
    server, protocol, _ = await world.server()
    assert protocol is not None
    _, _, _, credential = await world.issued(protocol)
    await get_redis().set(
        cache_key(protocol.id),
        json.dumps(
            {
                "sampled_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
                "peers": [
                    {
                        "public_key": credential.client_public_key,
                        "latest_handshake": None,
                        "rx_bytes": None,
                        "tx_bytes": None,
                    }
                ],
            }
        ),
    )
    (sample,) = (await ClientMetricsService(world.session).get_cached(server.id)).protocols
    assert sample.stale is stale
    (client,) = sample.clients
    assert client.latest_handshake is None
    assert client.downloaded_bytes is None and client.uploaded_bytes is None


@pytest.mark.parametrize("raw", [None, "not-json", '{"sampled_at":"invalid","peers":[]}'])
async def test_missing_or_corrupt_snapshot_is_unavailable_instead_of_zero(world: World, raw: str | None) -> None:
    server, protocol, _ = await world.server()
    assert protocol is not None
    if raw is not None:
        await get_redis().set(cache_key(protocol.id), raw)
    (sample,) = (await ClientMetricsService(world.session).get_cached(server.id)).protocols
    assert sample.sampled_at is None and sample.clients == [] and not sample.stale


async def test_revoked_credential_is_not_returned_from_an_older_snapshot(world: World) -> None:
    server, protocol, fake = await world.server()
    assert protocol is not None
    _, _, _, credential = await world.issued(protocol)
    fake.transfers[credential.client_public_key] = (1, 2)
    assert await MetricsCollector().collect(server.id)
    credential.revoked_at = datetime.now(timezone.utc)
    await world.session.commit()
    assert (await ClientMetricsService(world.session).get_cached(server.id)).protocols[0].clients == []


async def test_unknown_server_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await ClientMetricsService(world.session).get_cached(uuid4())


async def test_same_public_key_on_different_servers_keeps_its_own_counters(world: World) -> None:
    server, protocol, fake = await world.server()
    other, other_protocol, other_fake = await world.server()
    assert protocol is not None and other_protocol is not None
    _, _, _, credential = await world.issued(protocol)
    _, _, _, other_credential = await world.issued(other_protocol)
    other_credential.client_public_key = credential.client_public_key
    await world.session.commit()
    for protocol_id, rx in [(protocol.id, 123), (other_protocol.id, 999)]:
        await get_redis().set(
            cache_key(protocol_id),
            json.dumps(
                {
                    "sampled_at": datetime.now(timezone.utc).isoformat(),
                    "peers": [
                        {
                            "public_key": credential.client_public_key,
                            "latest_handshake": None,
                            "rx_bytes": rx,
                            "tx_bytes": 0,
                        }
                    ],
                }
            ),
        )
    before = (list(fake.commands), list(other_fake.commands))
    first = (await ClientMetricsService(world.session).get_cached(server.id)).protocols[0].clients[0]
    second = (await ClientMetricsService(world.session).get_cached(other.id)).protocols[0].clients[0]
    assert (first.uploaded_bytes, second.uploaded_bytes) == (123, 999)
    assert first.credential_id == credential.id and second.credential_id == other_credential.id
    assert before == (fake.commands, other_fake.commands)


async def test_expired_snapshot_is_not_displayed_as_current_data(world: World) -> None:
    server, protocol, _ = await world.server()
    assert protocol is not None
    await get_redis().set(
        cache_key(protocol.id),
        json.dumps({"sampled_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), "peers": []}),
    )
    (sample,) = (await ClientMetricsService(world.session).get_cached(server.id)).protocols
    assert sample.sampled_at is None and sample.clients == []


async def test_unavailable_redis_does_not_make_read_fail(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    server, _, _ = await world.server()

    async def failed_get(*args: object, **kwargs: object) -> str:
        raise RedisError("unavailable")

    monkeypatch.setattr(get_redis(), "get", failed_get)
    (sample,) = (await ClientMetricsService(world.session).get_cached(server.id)).protocols
    assert sample.sampled_at is None and sample.clients == []
