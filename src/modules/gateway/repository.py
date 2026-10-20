#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from ipaddress import IPv4Address, ip_interface, ip_network
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT
from modules.access.models.vpn_credential import VPNCredential
from modules.gateway.models.gateway_assignment import AssignReason, GatewayAssignment
from modules.gateway.models.gateway_exit import MAX_SLOT, ExitState, GatewayExit
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.protocols.models.protocol import VPNProtocol
from modules.servers.models.server import VPNServer

# Состояния, в которых у выхода есть живой внутренний клиент на выходе: sync его не трогает и возвращает, если потерян.
logger = logging.getLogger("gateway.assignments")
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

    async def exit_hosts(self, gateway_protocol_id: UUID) -> list[str]:
        """Адреса (поле host) серверов-выходов шлюза."""
        query = (
            select(VPNServer.host)
            .join(VPNProtocol, VPNProtocol.server_id == VPNServer.id)
            .join(GatewayExit, GatewayExit.exit_protocol_id == VPNProtocol.id)
            .where(GatewayExit.gateway_protocol_id == gateway_protocol_id)
        )
        return list((await self.session.execute(query)).scalars())

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


class GatewayAssignmentRepository:
    """Назначения выходов клиентам шлюза. «Живые» назначения - у не отозванных credential'ов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, credential_id: UUID) -> GatewayAssignment | None:
        return await self.session.get(GatewayAssignment, credential_id)

    async def set(self, credential_id: UUID, exit_id: UUID, reason: AssignReason) -> GatewayAssignment:
        """Назначить или перенести клиента (одна строка на credential)."""
        assignment = await self.get(credential_id)
        if assignment is None:
            assignment = GatewayAssignment(credential_id=credential_id, exit_id=exit_id, reason=reason)
            self.session.add(assignment)
        else:
            assignment.exit_id, assignment.reason = exit_id, reason
            assignment.assigned_at = datetime.now(timezone.utc)
        await self.session.flush()
        return assignment

    async def delete(self, assignment: GatewayAssignment) -> None:
        await self.session.delete(assignment)
        await self.session.flush()

    async def counts(self, gateway_protocol_id: UUID) -> dict[UUID, int]:
        """exit_id -> число живых клиентов, назначенных на выход."""
        query = (
            select(GatewayAssignment.exit_id, func.count())
            .join(GatewayExit, GatewayExit.id == GatewayAssignment.exit_id)
            .join(VPNCredential, VPNCredential.id == GatewayAssignment.credential_id)
            .where(GatewayExit.gateway_protocol_id == gateway_protocol_id, VPNCredential.revoked_at.is_(None))
            .group_by(GatewayAssignment.exit_id)
        )
        return {exit_id: count for exit_id, count in (await self.session.execute(query)).all()}

    async def list_for_gateway(self, gateway_protocol_id: UUID) -> Sequence[tuple[GatewayAssignment, int]]:
        """Живые назначения шлюза вместе со слотом выхода: старые первыми."""
        query = (
            select(GatewayAssignment, GatewayExit.slot)
            .join(GatewayExit, GatewayExit.id == GatewayAssignment.exit_id)
            .join(VPNCredential, VPNCredential.id == GatewayAssignment.credential_id)
            .where(GatewayExit.gateway_protocol_id == gateway_protocol_id, VPNCredential.revoked_at.is_(None))
            .order_by(GatewayAssignment.assigned_at, GatewayAssignment.credential_id)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(query)).all()]

    async def list_live(self, gateway_protocol_id: UUID) -> Sequence[GatewayAssignment]:
        """Живые назначения шлюза (без слота): для плана failover."""
        query = (
            select(GatewayAssignment)
            .join(GatewayExit, GatewayExit.id == GatewayAssignment.exit_id)
            .join(VPNCredential, VPNCredential.id == GatewayAssignment.credential_id)
            .where(GatewayExit.gateway_protocol_id == gateway_protocol_id, VPNCredential.revoked_at.is_(None))
            .order_by(GatewayAssignment.credential_id)
        )
        return (await self.session.execute(query)).scalars().all()

    async def list_for_exit(self, exit_id: UUID) -> Sequence[GatewayAssignment]:
        query = (
            select(GatewayAssignment)
            .where(GatewayAssignment.exit_id == exit_id)
            .order_by(GatewayAssignment.assigned_at, GatewayAssignment.credential_id)
        )
        return (await self.session.execute(query)).scalars().all()

    async def build_map(self, gateway_protocol_id: UUID) -> dict[IPv4Address, int]:
        """См. build_map_checked; без счётчика пропущенных."""
        return (await self.build_map_checked(gateway_protocol_id))[0]

    async def build_map_checked(self, gateway_protocol_id: UUID) -> tuple[dict[IPv4Address, int], int]:
        """Желаемая карта шлюза целиком: адрес живого клиента -> слот его выхода (ACTIVE и DRAINING: пока выход не снят,
        его клиенты обслуживаются). Строится из БД заново, не инкрементом: переиспользованный адрес не наследует чужое
        назначение, отозванный клиент в карту не попадает. Адрес, который не разбирается, пропускается."""
        query = (
            select(VPNCredential.client_address, GatewayExit.slot)
            .select_from(GatewayAssignment)
            .join(GatewayExit, GatewayExit.id == GatewayAssignment.exit_id)
            .join(VPNCredential, VPNCredential.id == GatewayAssignment.credential_id)
            .where(
                GatewayExit.gateway_protocol_id == gateway_protocol_id,
                GatewayExit.state.in_((ExitState.ACTIVE, ExitState.DRAINING)),
                VPNCredential.revoked_at.is_(None),
            )
        )
        network = ip_network(GATEWAY_LAYOUT.tunnel_subnet)
        reserved = {
            network.network_address,
            network.broadcast_address,
            ip_interface(GATEWAY_LAYOUT.interface_address).ip,
        }
        result: dict[IPv4Address, int] = {}
        dropped = 0
        for address, slot in (await self.session.execute(query.order_by(GatewayAssignment.credential_id))).all():
            try:
                parsed = IPv4Address(address)
            except ValueError:
                parsed = None
            # Адрес вне подсети клиентов, сети/broadcast/адрес самого шлюза и повтор адреса в карту не попадают
            # (они уронили бы весь файл правил): пропускаем и считаем, не логируя сами адреса.
            if parsed is None or parsed not in network or parsed in reserved or parsed in result:
                dropped += 1
                continue
            result[parsed] = slot
        if dropped:
            logger.warning("assignments skipped as invalid", extra={"count": dropped})
        return result, dropped

    async def delete_dead(self, gateway_protocol_id: UUID) -> int:
        """Убрать назначения отозванных credential'ов (по FK они уходят только вместе с удалением credential'а)."""
        ids = (
            (
                await self.session.execute(
                    select(GatewayAssignment.credential_id)
                    .join(GatewayExit, GatewayExit.id == GatewayAssignment.exit_id)
                    .join(VPNCredential, VPNCredential.id == GatewayAssignment.credential_id)
                    .where(
                        GatewayExit.gateway_protocol_id == gateway_protocol_id, VPNCredential.revoked_at.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        for credential_id in ids:
            assignment = await self.get(credential_id)
            if assignment is not None:
                await self.session.delete(assignment)
        await self.session.flush()
        return len(ids)
