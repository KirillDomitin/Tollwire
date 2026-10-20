#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.gateway.models.gateway_exit import MAX_SLOT, ExitState, GatewayExit
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.protocols.models.protocol import VPNProtocol
from modules.servers.models.server import VPNServer

# Состояния, в которых у выхода есть живой внутренний клиент на выходе: sync его не трогает и возвращает, если потерян.
LIVE_CLIENT_STATES = (ExitState.PEER_CREATED, ExitState.ACTIVE, ExitState.DRAINING)
_EXIT_JOB_TYPES = (JobType.GATEWAY_ADD_EXIT, JobType.GATEWAY_REMOVE_EXIT)


class GatewayExitRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, exit_id: UUID) -> GatewayExit | None:
        return await self.session.get(GatewayExit, exit_id)

    async def get_for_gateway(self, gateway_protocol_id: UUID, exit_id: UUID) -> GatewayExit | None:
        query = select(GatewayExit).where(
            GatewayExit.id == exit_id, GatewayExit.gateway_protocol_id == gateway_protocol_id
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_by_pair(self, gateway_protocol_id: UUID, exit_protocol_id: UUID) -> GatewayExit | None:
        query = select(GatewayExit).where(
            GatewayExit.gateway_protocol_id == gateway_protocol_id, GatewayExit.exit_protocol_id == exit_protocol_id
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_by_gateway(self, gateway_protocol_id: UUID) -> Sequence[GatewayExit]:
        query = select(GatewayExit).where(GatewayExit.gateway_protocol_id == gateway_protocol_id)
        return (await self.session.execute(query.order_by(GatewayExit.slot))).scalars().all()

    async def list_with_server_names(self, gateway_protocol_id: UUID) -> Sequence[tuple[GatewayExit, UUID, str]]:
        """Выходы шлюза вместе с id и именем сервера-выхода (для GET)."""
        query = (
            select(GatewayExit, VPNServer.id, VPNServer.name)
            .join(VPNProtocol, VPNProtocol.id == GatewayExit.exit_protocol_id)
            .join(VPNServer, VPNServer.id == VPNProtocol.server_id)
            .where(GatewayExit.gateway_protocol_id == gateway_protocol_id)
            .order_by(GatewayExit.slot)
        )
        return [(row[0], row[1], row[2]) for row in (await self.session.execute(query)).all()]

    async def list_live_for_exit_protocol(self, exit_protocol_id: UUID) -> Sequence[GatewayExit]:
        """Выходы, чей внутренний клиент сейчас живёт на этом протоколе выхода (для sync выхода)."""
        query = select(GatewayExit).where(
            GatewayExit.exit_protocol_id == exit_protocol_id,
            GatewayExit.state.in_(LIVE_CLIENT_STATES),
            GatewayExit.client_public_key.is_not(None),
        )
        return (await self.session.execute(query)).scalars().all()

    async def free_slot(self, gateway_protocol_id: UUID) -> int | None:
        """Наименьший свободный слот 1..MAX_SLOT или None, если все заняты."""
        query = select(GatewayExit.slot).where(GatewayExit.gateway_protocol_id == gateway_protocol_id)
        taken = set((await self.session.execute(query)).scalars().all())
        return next((slot for slot in range(1, MAX_SLOT + 1) if slot not in taken), None)

    async def lock_gateway(self, gateway_protocol_id: UUID) -> None:
        """Блокировка строки протокола шлюза до конца транзакции: два одновременных добавления не возьмут один слот."""
        await self.session.execute(
            select(VPNProtocol.id).where(VPNProtocol.id == gateway_protocol_id).with_for_update()
        )

    async def add(self, gateway_exit: GatewayExit) -> GatewayExit:
        self.session.add(gateway_exit)
        await self.session.flush()
        return gateway_exit

    async def delete(self, gateway_exit: GatewayExit) -> None:
        await self.session.delete(gateway_exit)

    async def exists_for_exit_server(self, server_id: UUID) -> bool:
        """Сервер используется как выход какого-либо шлюза (его AMNEZIA_WG-протокол есть в gateway_exits)."""
        query = select(
            exists().where(
                GatewayExit.exit_protocol_id == VPNProtocol.id,
                VPNProtocol.server_id == server_id,
            )
        )
        return bool((await self.session.execute(query)).scalar())

    async def has_unfinished_job(self, exit_id: UUID) -> bool:
        """По выходу есть незавершённая (ожидает / идёт) задача добавления или снятия."""
        query = select(
            exists().where(
                Job.type.in_(_EXIT_JOB_TYPES),
                Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
                Job.params["exit_id"].astext == str(exit_id),
            )
        )
        return bool((await self.session.execute(query)).scalar())
