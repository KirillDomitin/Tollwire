#!/usr/bin/python
# -*- coding: utf-8 -*-
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError, SSHConnectionError, SSHError, SSHTimeoutError
from infrastructure.vpn.amnezia import AmneziaInstaller
from infrastructure.vpn.provider import HealthCheck, VPNProvider
from modules.protocols.models.protocol import APP_PROTOCOLS, ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.servers.models.server import HealthStatus, VPNServer
from modules.servers.reachability import mark_reachable, mark_unreachable
from modules.servers.repository import ServerRepository
from modules.servers.service import ServerConnectionService


class ServerHealthService:
    """Проверяет здоровье сервера: SSH, Docker, интернет, протоколы. На сервере ничего не меняет.

    Блокировку сервера не берёт — только чтение, не должен ждать выдачу/отзыв клиентов.
    Отчёт сохраняется в Job.result, сводка — в VPNServer.health_status/last_health_at.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.servers = ServerRepository(session)
        self.protocols = ProtocolRepository(session)
        self.connections = ServerConnectionService(session)

    async def check(self, server_id: UUID) -> dict[str, Any]:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError(f"Server '{server_id}' not found")
        ssh_client = await self.connections.create_ssh_client(server_id)
        checked_at = datetime.now(timezone.utc)

        started = time.monotonic()
        try:
            await ssh_client.connect()
        except SSHError as exc:
            # Недоступный сервер — это результат проверки, а не сбой задачи: отчёт сохраняется, задача SUCCESS.
            if isinstance(exc, (SSHConnectionError, SSHTimeoutError)):
                mark_unreachable(server)
            return await self._save(server, checked_at, [HealthCheck("ssh", False, str(exc))], [])
        ssh_ms = round((time.monotonic() - started) * 1000)

        checks = [HealthCheck("ssh", True, f"connected in {ssh_ms} ms")]
        protocols: list[dict[str, Any]] = []
        async with ssh_client:
            installer = AmneziaInstaller(ssh_client)
            docker = await installer.docker_version()
            checks.append(HealthCheck("docker", docker is not None, docker or "not installed"))
            latency = await installer.internet_latency_ms()
            checks.append(
                HealthCheck("internet", latency is not None, f"{latency} ms" if latency is not None else "no access")
            )
            mark_reachable(server, docker_installed=docker is not None)
            if docker:
                daemon = await installer.docker_daemon_active()
                checks.append(HealthCheck("docker_daemon", daemon, "active" if daemon else "inactive"))
                tracked = {protocol.type: protocol for protocol in await self.protocols.list_by_server_id(server_id)}
                for protocol_type in ProtocolType:
                    provider = self.connections.build_provider(ssh_client, protocol_type)
                    report = await self._check_protocol(provider, protocol_type, tracked.get(protocol_type))
                    if report:
                        protocols.append(report)

        return await self._save(server, checked_at, checks, protocols, internet_latency_ms=latency)

    async def _check_protocol(
        self, provider: VPNProvider, protocol_type: ProtocolType, protocol: VPNProtocol | None
    ) -> dict[str, Any] | None:
        health = await provider.health_check()
        checks = list(health.checks)
        if protocol is None:
            if not health.installed:
                return None  # протокол не ставили — проверять нечего
            if protocol_type in APP_PROTOCOLS:
                # Установка AmneziaVPN-приложения — не поломка: панель её не ставила и не отслеживает, пока клиентов
                # не подхватили. Иначе сервер с приложением получал бы DEGRADED навсегда.
                checks.append(HealthCheck("tracked_in_db", True, "installed by the AmneziaVPN app, not managed"))
            else:
                checks.append(HealthCheck("tracked_in_db", False, "installed on server but not tracked in DB"))
        elif health.port is not None:
            checks.append(
                HealthCheck(
                    "port_matches_db", health.port == protocol.port, f"db {protocol.port}, server {health.port}"
                )
            )
        return {
            "type": protocol_type.value,
            "in_db": protocol is not None,
            "port": health.port,
            "clients": {"total": health.clients_total, "active": health.clients_active},
            "checks": [asdict(check) for check in checks],
        }

    async def _save(
        self,
        server: VPNServer,
        checked_at: datetime,
        checks: list[HealthCheck],
        protocols: list[dict[str, Any]],
        internet_latency_ms: int | None = None,
    ) -> dict[str, Any]:
        ssh_ok = checks[0].ok
        all_ok = all(check.ok for check in checks) and all(c["ok"] for p in protocols for c in p["checks"])
        status = HealthStatus.DOWN if not ssh_ok else HealthStatus.OK if all_ok else HealthStatus.DEGRADED
        server.health_status = status
        server.last_health_at = checked_at
        await self.session.commit()
        return {
            "status": status.value,
            "checked_at": checked_at.isoformat(),
            "server_status": server.status.value,
            "internet_latency_ms": internet_latency_ms,
            "checks": [asdict(check) for check in checks],
            "protocols": protocols,
        }
