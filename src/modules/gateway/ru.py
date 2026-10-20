#!/usr/bin/python
# -*- coding: utf-8 -*-
import contextlib
import logging
from ipaddress import IPv4Network
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import (
    CredentialNotFound,
    GatewayExitError,
    InvalidDataError,
    JobAlreadyRunningError,
    NotFoundError,
)
from core.ssh_exception import SSHServerError
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import RuStatus, render_ru_prefixes, ru_digest
from infrastructure.vpn.provider import HealthCheck
from modules.audit.actor import AuditActor
from modules.geoip.parser import GeoipError
from modules.geoip.service import GeoipService
from modules.jobs.models.job import Job, JobType
from modules.jobs.repository import JobRepository
from modules.jobs.service import JobService
from modules.protocols.models.protocol import ProtocolType
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import CredentialRepository, ServerRepository
from modules.servers.service import ServerConnectionService

logger = logging.getLogger("gateway.ru")


class GatewayRuService:
    """«Россия напрямую» на шлюзе (этап 24.4): применение эффективного списка (активный список ∪ DIRECT − TUNNEL) и
    сверка с тем, что стоит на шлюзе. Применение идёт задачей GATEWAY_APPLY_RU под ServerLock шлюза."""

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.servers = ServerRepository(session)
        self.credentials = CredentialRepository(session)
        self.protocols = ProtocolRepository(session)
        self.geoip = GeoipService(session, actor)
        self.connections = ServerConnectionService(session)

    async def schedule_apply(self, server_id: UUID) -> Job:
        """POST /servers/{id}/gateway/ru/apply: быстрые проверки по БД и задача (202). 404 - нет сервера; 409 - нет
        SSH-учётки, шлюза, активного списка или такая же задача уже не завершена."""
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError("Server not found")
        if not await self.credentials.get_by_server_id(server_id):
            raise CredentialNotFound("SSH credentials are not set for this server")
        if await self.protocols.get_by_server_and_type(server_id, ProtocolType.AMNEZIA_GATEWAY) is None:
            raise GatewayExitError("The gateway is not installed on this server")
        if await self.geoip.repository.get_active() is None:
            raise GatewayExitError("There is no active geoip list: update or upload it first")
        if await JobRepository(self.session).has_unfinished_for_server(JobType.GATEWAY_APPLY_RU, server_id):
            raise JobAlreadyRunningError("The Russian routes are already being applied on this gateway")
        return await JobService(self.session, self.actor).create(JobType.GATEWAY_APPLY_RU, server_id)

    async def _effective(self, server_id: UUID) -> list[IPv4Network] | None:
        """Эффективный список для этого шлюза. Испорченные данные в БД - GatewayExitError с фиксированным текстом (в
        исключениях разбора могут быть строки списка)."""
        try:
            return await self.geoip.effective_prefixes(server_id)
        except GeoipError as exc:
            raise GatewayExitError(f"The stored geoip list cannot be used: {exc}") from None
        except ValueError:
            raise GatewayExitError("The stored geoip list or an override is damaged") from None

    async def run_apply(self, job: Job) -> dict[str, Any]:
        """Исполняет воркер под ServerLock шлюза: эффективный список -> `apply_ru`. Результат: маршруты и таблица."""
        if job.server_id is None:
            raise InvalidDataError("Gateway job has no server")
        prefixes = await self._effective(job.server_id)
        if prefixes is None:
            raise GatewayExitError("There is no active geoip list")
        provider, ssh_client = await self._provider(job.server_id)
        async with ssh_client:
            status = await provider.apply_ru(prefixes)
        return {"routes": status.routes, "table": status.table, "prefixes": len(prefixes)}

    @staticmethod
    def _in_sync(status: RuStatus, prefixes: list[IPv4Network]) -> bool:
        """На шлюзе применён именно этот список: отпечаток из ru.applied совпал и маршрутов столько, сколько префиксов
        (одного числа мало: другой список того же размера прошёл бы незамеченным)."""
        return status.routes == len(prefixes) and status.digest == ru_digest(render_ru_prefixes(prefixes))

    async def ensure(self, provider: AmneziaGatewayProvider, issues: list[str], server_id: UUID) -> dict[str, Any]:
        """Сверка (sync, установка): есть активный список, а на шлюзе применён другой (отпечаток или число маршрутов
        не совпали: таблица пуста, устарела или от другого списка) - применить. Нет активного списка - ничего и не
        ошибка. Сбой применения или испорченный список в БД - issue."""
        try:
            prefixes = await self._effective(server_id)
        except GatewayExitError as exc:
            issues.append(str(exc))
            return {"active_list": True, "error": True}
        if prefixes is None:
            return {"active_list": False}
        status = await provider.ru_status()
        report: dict[str, Any] = {"active_list": True, "expected": len(prefixes), "routes": status.routes}
        if self._in_sync(status, prefixes):
            return {**report, "applied": False}
        try:
            applied = await provider.apply_ru(prefixes)
        except (SSHServerError, ValueError) as exc:
            issues.append(f"Failed to apply the Russian routes: {str(exc)[:200]}")
            return {**report, "applied": False, "error": True}
        return {**report, "applied": True, "routes": applied.routes}

    async def health_check(self, provider: AmneziaGatewayProvider, server_id: UUID) -> HealthCheck | None:
        """Проверка `ru` для health: только когда есть активный список (иначе шлюз без списка вечно был бы DEGRADED).
        Пройдена, если применён именно эффективный список (отпечаток и число маршрутов)."""
        try:
            prefixes = await self._effective(server_id)
        except GatewayExitError:
            return HealthCheck("ru", False, "the stored geoip list cannot be used")
        if prefixes is None:
            return None
        try:
            status = await provider.ru_status()
        except SSHServerError:
            return HealthCheck("ru", False, "failed to read the Russian routes")
        if self._in_sync(status, prefixes):
            return HealthCheck("ru", True, f"{status.routes} routes")
        return HealthCheck("ru", False, f"{status.routes} routes, expected {len(prefixes)} of the current list")

    async def ensure_server(self, server_id: UUID) -> None:
        """После установки шлюза (воркер): вернуть маршруты из активного списка, а если списка ещё нет - поставить его
        получение (GEOIP_UPDATE, без дублей). Сбой не роняет установку - только лог (сверку повторит sync)."""
        protocol = await self.protocols.get_by_server_and_type(server_id, ProtocolType.AMNEZIA_GATEWAY)
        if protocol is None:
            return
        try:
            if await self.geoip.repository.get_active() is None:
                with contextlib.suppress(JobAlreadyRunningError):
                    await self.geoip.schedule_update()
                return
            provider, ssh_client = await self._provider(server_id)
            async with ssh_client:
                await self.ensure(provider, [], server_id)
        except Exception as exc:  # noqa: BLE001 - установка уже закоммичена, сверку повторит sync
            logger.warning("russian routes were not restored after install", extra={"error": str(exc)[:200]})

    async def _provider(self, server_id: UUID) -> tuple[AmneziaGatewayProvider, Any]:
        ssh_client = await self.connections.create_ssh_client(server_id)
        provider = self.connections.build_provider(ssh_client, ProtocolType.AMNEZIA_GATEWAY)
        assert isinstance(provider, AmneziaGatewayProvider)
        return provider, ssh_client
