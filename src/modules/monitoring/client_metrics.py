"""Short-lived runtime peer observations; HTTP reads never connect to VPN servers."""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from core.redis import get_redis
from infrastructure.vpn.provider import VPNClientStats
from modules.access.repository import VPNCredentialRepository
from modules.monitoring.schemas import ClientMetric, ProtocolClientMetrics, ServerClientMetrics, UserDeviceMetric
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import ServerRepository

logger = logging.getLogger("monitoring")
RETENTION_SECONDS = 86400
STALE_SECONDS = 300


def cache_key(protocol_id: UUID) -> str:
    return f"client_metrics:{protocol_id}"


class PeerSample(BaseModel):
    public_key: str
    latest_handshake: AwareDatetime | None
    rx_bytes: int | None = Field(ge=0, le=2**63 - 1)
    tx_bytes: int | None = Field(ge=0, le=2**63 - 1)


class ClientSnapshot(BaseModel):
    sampled_at: AwareDatetime
    peers: list[PeerSample] = Field(max_length=4096)


def decode_snapshot(raw: str | bytes | None, now: datetime) -> ClientSnapshot | None:
    if raw is None or len(raw) > 2 * 1024 * 1024:
        return None
    try:
        snapshot = ClientSnapshot.model_validate_json(raw)
    except (ValueError, TypeError):
        return None
    return snapshot if (now - snapshot.sampled_at).total_seconds() < RETENTION_SECONDS else None


def is_stale(snapshot: ClientSnapshot, now: datetime) -> bool:
    return not 0 <= (now - snapshot.sampled_at).total_seconds() < STALE_SECONDS


async def save_snapshot(protocol_id: UUID, sampled_at: datetime, peers: list[VPNClientStats]) -> None:
    snapshot = ClientSnapshot(
        sampled_at=sampled_at,
        peers=[
            PeerSample(
                public_key=peer.public_key,
                latest_handshake=peer.latest_handshake,
                rx_bytes=peer.rx_bytes,
                tx_bytes=peer.tx_bytes,
            )
            for peer in peers
        ],
    )
    try:
        await get_redis().set(cache_key(protocol_id), snapshot.model_dump_json(), ex=RETENTION_SECONDS)
    except RedisError:
        logger.warning("client metrics cache unavailable")


class ClientMetricsService:
    def __init__(self, session: AsyncSession):
        self.servers = ServerRepository(session)
        self.protocols = ProtocolRepository(session)
        self.credentials = VPNCredentialRepository(session)

    async def by_user(self) -> dict[UUID, list[UserDeviceMetric]]:
        devices = await self.credentials.live_device_metrics_identity()
        protocol_ids = list(dict.fromkeys(row[1] for row in devices))
        now = datetime.now(timezone.utc)
        snapshots: dict[UUID, tuple[bool, dict[str, PeerSample]]] = {}
        if protocol_ids:
            try:
                samples = await get_redis().mget([cache_key(protocol_id) for protocol_id in protocol_ids])
            except RedisError:
                samples = []
            for protocol_id, raw in zip(protocol_ids, samples):
                snapshot = decode_snapshot(raw, now)
                if snapshot is not None:
                    snapshots[protocol_id] = (
                        is_stale(snapshot, now),
                        {peer.public_key: peer for peer in snapshot.peers},
                    )

        result: defaultdict[UUID, list[UserDeviceMetric]] = defaultdict(list)
        for user_id, protocol_id, credential_id, server_name, device_name, public_key in devices:
            stale, peers = snapshots.get(protocol_id, (False, {}))
            peer = peers.get(public_key) if public_key is not None else None
            result[user_id].append(
                UserDeviceMetric(
                    credential_id=credential_id,
                    server_name=server_name,
                    device_name=device_name,
                    latest_handshake=peer.latest_handshake if peer else None,
                    downloaded_bytes=peer.tx_bytes if peer else None,
                    uploaded_bytes=peer.rx_bytes if peer else None,
                    stale=stale and peer is not None,
                )
            )
        return result

    async def get_cached(self, server_id: UUID) -> ServerClientMetrics:
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError("Server not found")
        protocols = await self.protocols.list_by_server_id(server_id)
        credentials = await self.credentials.live_by_server(server_id)
        now = datetime.now(timezone.utc)
        result: list[ProtocolClientMetrics] = []
        for protocol in protocols:
            item = ProtocolClientMetrics(protocol_id=protocol.id)
            result.append(item)
            try:
                raw = await get_redis().get(cache_key(protocol.id))
            except RedisError:
                continue
            snapshot = decode_snapshot(raw, now)
            if snapshot is None:
                continue
            item.sampled_at = snapshot.sampled_at
            item.stale = is_stale(snapshot, now)
            by_key = {peer.public_key: peer for peer in snapshot.peers}
            for credential, protocol_id in credentials:
                if protocol_id != protocol.id or credential.client_public_key is None:
                    continue
                peer = by_key.get(credential.client_public_key)
                if peer is not None:
                    item.clients.append(
                        ClientMetric(
                            credential_id=credential.id,
                            latest_handshake=peer.latest_handshake,
                            downloaded_bytes=peer.tx_bytes,
                            uploaded_bytes=peer.rx_bytes,
                        )
                    )
        return ServerClientMetrics(server_id=server_id, protocols=result)
