import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from infrastructure.ssh.client import SSHClient
from modules.access.models.access_grant import AccessGrant
from modules.access.models.vpn_credential import VPNCredential
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.protocols.models.protocol import ProtocolType
from modules.servers.repository import ServerRepository
from modules.servers.service import ServerConnectionService

logger = logging.getLogger("servers.discovery")


class ServerDiscoveryService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.servers = ServerRepository(session)
        self.connections = ServerConnectionService(session)

    async def discover(self, server_id: UUID) -> dict[str, Any]:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError(f"Server '{server_id}' not found")
        ssh_client = await self.connections.create_ssh_client(server_id)
        async with ssh_client:
            return await self.discover_with(ssh_client)

    async def discover_with(self, ssh_client: SSHClient) -> dict[str, Any]:
        protocols = []
        for protocol_type in ProtocolType:
            found = await self.connections.build_provider(ssh_client, protocol_type).discover()
            if found is None:
                continue
            protocols.append(
                {
                    "type": protocol_type.value,
                    "container": found.container,
                    "state": found.state.value,
                    "image": found.image,
                    "port": found.port,
                    "clients": [
                        {
                            "public_key": c.public_key,
                            "address": c.address,
                            "name": c.name,
                            "latest_handshake": c.latest_handshake.isoformat() if c.latest_handshake else None,
                            "supported": c.supported,
                        }
                        for c in found.clients
                    ],
                }
            )
        return {"protocols": protocols}

    async def latest(self, server_id: UUID) -> dict[str, Any]:
        """Отчёт последней успешной задачи поиска (DISCOVER_VPN или INSTALL_SERVER) со статусом каждого клиента.
        Отчёта нет — `checked_at=None`, `protocols=[]`. Читает только БД, SSH не открывает."""
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError(f"Server '{server_id}' not found")
        job = (
            await self.session.execute(
                select(Job)
                .where(
                    Job.server_id == server_id,
                    Job.type.in_([JobType.DISCOVER_VPN, JobType.INSTALL_SERVER]),  # установка тоже ищет Amnezia
                    Job.status == JobStatus.SUCCESS,
                    Job.result.is_not(None),
                )
                .order_by(Job.finished_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None or not job.result:
            return {"checked_at": None, "protocols": []}
        rows = (
            await self.session.execute(
                select(VPNCredential.client_public_key, VPNCredential.revoked_at)
                .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
                .where(AccessGrant.server_id == server_id)
            )
        ).all()
        live = {key for key, revoked in rows if revoked is None}
        revoked_keys = {key for key, revoked in rows if revoked is not None}
        protocols = []
        for protocol in job.result.get("protocols", []):
            clients = []
            for client in protocol.get("clients", []):
                if not client.get("supported", False):
                    status = "unsupported"
                elif client.get("public_key") in live:
                    status = "in_panel"
                elif client.get("public_key") in revoked_keys:
                    status = "revoked_in_panel"
                else:
                    status = "new"
                clients.append({**client, "status": status})
            protocols.append({**protocol, "clients": clients})
        checked_at: datetime | None = job.finished_at
        return {"checked_at": checked_at, "protocols": protocols}
