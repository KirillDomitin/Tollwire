"""Read-only availability observations and explicit protocol image updates."""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.exceptions import NotFoundError, ProtocolUpdateError
from core.redis import get_redis
from core.ssh_exception import DockerError, SSHServerError
from infrastructure.vpn.provider import ProviderImageInfo
from infrastructure.vpn.registry import AmneziaImageRegistry, RegistryError, RegistryImageInfo
from modules.audit.actor import AuditActor
from modules.jobs.models.job import JobType
from modules.jobs.repository import JobRepository
from modules.protocols.models.protocol import INSTALLABLE_PROTOCOLS, ProtocolType
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import ServerRepository
from modules.servers.schemas import ProtocolUpdateObservation
from modules.servers.service import ServerConnectionService

RETENTION_SECONDS = 7 * 86400
STALE_SECONDS = 26 * 3600
logger = logging.getLogger("protocol_updates")


class ImageRegistry(Protocol):
    async def latest_for(self, image: ProviderImageInfo) -> RegistryImageInfo: ...


def cache_key(protocol_id: UUID | str) -> str:
    return f"protocol_update:observation:{protocol_id}"


def generation_key(server_id: UUID) -> str:
    return f"protocol_update:generation:{server_id}"


def active_key(server_id: UUID) -> str:
    return f"protocol_update:active:{server_id}"


def blank(protocol_id: UUID, kind: ProtocolType) -> dict[str, Any]:
    return {
        "protocol_id": str(protocol_id),
        "protocol_type": kind.value,
        "status": "unknown" if kind in INSTALLABLE_PROTOCOLS else "unsupported",
        "stale": False,
        "checked_at": None,
        "last_attempt_at": None,
        "current_image_id": None,
        "target_image_id": None,
        "target_digest": None,
        "reason": None,
    }


class ProtocolUpdateCheckService:
    def __init__(
        self,
        session: AsyncSession,
        registry: ImageRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.session = session
        self.registry = registry or AmneziaImageRegistry()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _protocols(self, server_id: UUID) -> list[tuple[UUID, ProtocolType]]:
        if not await ServerRepository(self.session).get_by_id(server_id):
            raise NotFoundError("Server not found")
        return [(p.id, p.type) for p in await ProtocolRepository(self.session).list_by_server_id(server_id)]

    async def _cached(self, protocol_id: UUID, kind: ProtocolType, server_id: UUID) -> dict[str, Any]:
        empty = blank(protocol_id, kind)
        if kind not in INSTALLABLE_PROTOCOLS:
            return empty
        try:
            raw = await get_redis().get(cache_key(protocol_id))
            if raw is None:
                return empty
            saved = json.loads(raw)
            item = ProtocolUpdateObservation.model_validate(saved).model_dump(mode="json")
            if str(item["protocol_id"]) != str(protocol_id) or item["protocol_type"] != kind.value:
                return empty
            if item["checked_at"]:
                age = self.clock() - datetime.fromisoformat(item["checked_at"].replace("Z", "+00:00"))
                if age.total_seconds() >= RETENTION_SECONDS:
                    return empty
                item["stale"] = item["stale"] or age.total_seconds() > STALE_SECONDS
            if saved.get("_generation") != await get_redis().get(generation_key(server_id)):
                item.update(status="unknown", stale=True, reason="update_in_progress")
            return item
        except (RedisError, ValueError, TypeError, KeyError):
            return empty

    async def _busy(self, server_id: UUID) -> bool:
        return bool(await get_redis().exists(active_key(server_id)))

    async def get_cached(self, server_id: UUID) -> dict[str, Any]:
        protocols = await self._protocols(server_id)
        running = await JobRepository(self.session).has_running_for_server(JobType.UPDATE_PROTOCOL, server_id)
        # GET is deliberately cache-only: no SSH and no registry access.
        items = [await self._cached(pid, kind, server_id) for pid, kind in protocols]
        try:
            running = running or await self._busy(server_id)
        except RedisError:
            pass
        if running:
            for item in items:
                if item["status"] != "unsupported":
                    item.update(status="unknown", stale=True, reason="update_in_progress")
        return {"protocols": items}

    async def _save(self, item: dict[str, Any], generation: str | None) -> None:
        # Error attempts must not extend retention beyond the previous successful observation.
        age = 0.0
        if item["checked_at"]:
            age = (self.clock() - datetime.fromisoformat(item["checked_at"].replace("Z", "+00:00"))).total_seconds()
        ttl = max(1, int(RETENTION_SECONDS - max(age, 0)))
        await get_redis().set(cache_key(item["protocol_id"]), json.dumps({**item, "_generation": generation}), ex=ttl)

    async def check(self, server_id: UUID) -> dict[str, Any]:
        protocols = await self._protocols(server_id)
        running = await JobRepository(self.session).has_running_for_server(JobType.UPDATE_PROTOCOL, server_id)
        items = [await self._cached(pid, kind, server_id) for pid, kind in protocols]
        own = [item for item in items if item["status"] != "unsupported"]
        if not own:
            return {"protocols": items}
        raw_generation = await get_redis().get(generation_key(server_id))
        before = raw_generation.decode() if isinstance(raw_generation, bytes) else raw_generation
        running = running or await self._busy(server_id)
        now = self.clock().isoformat().replace("+00:00", "Z")
        if running:
            await self.session.commit()
            for item in own:
                item.update(status="unknown", stale=True, reason="update_in_progress", last_attempt_at=now)
                await self._save(item, before)
            return {"protocols": items}
        try:
            ssh = await ServerConnectionService(self.session).create_ssh_client(server_id)
            # Commit rather than rollback: this session may belong to a running Job.
            await self.session.commit()
            async with asyncio.timeout(75), ssh:
                for item in own:
                    kind = ProtocolType(item["protocol_type"])
                    try:
                        provider = ServerConnectionService(self.session).build_provider(ssh, kind)
                        image = await provider.image_info()
                        latest = await self.registry.latest_for(image)
                        installed_content = latest.current_image_id or image.image_id
                        item.update(
                            status="current" if installed_content == latest.image_id else "available",
                            stale=False,
                            checked_at=now,
                            last_attempt_at=now,
                            current_image_id=image.image_id,
                            target_image_id=latest.image_id,
                            target_digest=latest.digest,
                            reason=None,
                        )
                    except (SSHServerError, RegistryError, httpx.HTTPError, ValueError) as error:
                        self._failed(item, error, now)
        except (RedisError, SQLAlchemyError):
            raise
        except Exception as error:
            # Do not expose exception messages: SSH/registry responses can contain private material.
            await self.session.commit()
            for item in own:
                self._failed(item, error, now)
        # An update can start AND finish during a metadata request. Compare a persistent generation,
        # not just a transient busy flag, before publishing any observation.
        changed = before != await get_redis().get(generation_key(server_id)) or await self._busy(server_id)
        for item in own:
            if changed:
                item.update(status="unknown", stale=True, reason="update_in_progress")
            await self._save(item, before)
        return {"protocols": items}

    @staticmethod
    def _failed(item: dict[str, Any], error: Exception, now: str) -> None:
        if isinstance(error, RegistryError):
            reason = error.reason
        elif isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429:
            reason = "rate_limited"
        elif isinstance(error, httpx.HTTPError):
            reason = "registry_unavailable"
        elif isinstance(error, DockerError):
            reason = "container_missing"
        else:
            reason = "server_unreachable"
        item.update(status="unknown", stale=True, reason=reason, last_attempt_at=now)


class ProtocolUpdateService:
    """Called under ServerLock; never recreates VPNProtocol or client records."""

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor

    async def run(self, server_id: UUID, kind: ProtocolType) -> dict[str, Any]:
        protocol = await ProtocolRepository(self.session).get_by_server_and_type(server_id, kind)
        if protocol is None or kind not in INSTALLABLE_PROTOCOLS:
            raise NotFoundError("Installed panel protocol not found")
        protocol_id = protocol.id
        connection = ServerConnectionService(self.session)
        ssh = await connection.create_ssh_client(server_id)
        provider = connection.build_provider(ssh, kind)
        await self.session.commit()
        redis = get_redis()
        token = uuid4().hex
        await redis.incr(generation_key(server_id))
        await redis.set(active_key(server_id), token, ex=settings.stale_job_timeout_seconds)
        try:
            async with ssh:
                result = asdict(await provider.update())
            return {**result, "protocol_type": kind.value}
        except Exception as error:
            error_result = getattr(error, "job_result", None)
            safe: dict[str, Any] = {"protocol_type": kind.value}
            if isinstance(error_result, dict):
                allowed = {"changed", "previous_image_id", "target_image_id", "target_digest", "rollback", "recovered"}
                safe.update({key: value for key, value in error_result.items() if key in allowed})
            failure = ProtocolUpdateError("Protocol container update failed")
            failure.job_result = safe
            raise failure from None
        finally:
            # Invalidate even on rollback: registry latest may have changed during the operation.
            try:
                await redis.incr(generation_key(server_id))
                await redis.delete(cache_key(protocol_id))
                await redis.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
                    1,
                    active_key(server_id),
                    token,
                )
            except RedisError:
                # The initial generation increment already invalidates all older observations;
                # the busy marker also expires. Do not replace the actual rollback result.
                logger.warning("post-update availability cache cleanup failed")
