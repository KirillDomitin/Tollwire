#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from core.ssh_exception import CommandExecutionError
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import ExitTunnelStatus
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import AccessGrantRepository, VPNCredentialRepository
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.gateway.balancer import ExitCandidate, pick_exit
from modules.gateway.exit_health import record_exit_status
from modules.gateway.models.gateway_assignment import AssignReason
from modules.gateway.models.gateway_exit import ExitState, GatewayExit
from modules.gateway.repository import GatewayAssignmentRepository, GatewayExitRepository
from modules.gateway.tunnels import exit_tunnel
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.servers.service import ServerConnectionService

logger = logging.getLogger("gateway.assignments")


class GatewayAssignmentService:
    """Какой выход обслуживает какого клиента шлюза (этап 24.3).

    БД - источник правды: назначение пишется в `gateway_assignments`, а на шлюз уходит ПОЛНАЯ карта живых назначений
    (`apply_assignments`), собранная из БД заново, а не инкрементом: отозванный клиент и переиспользованный адрес
    не оставляют следов. Нет выхода - нет строки - нет метки - kill-switch: у клиента нет интернета, утечки нет.
    Вызывается воркером под ServerLock шлюза (выдача, отзыв, добавление и снятие выхода, sync).
    """

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.protocols = ProtocolRepository(session)
        self.exits = GatewayExitRepository(session)
        self.assignments = GatewayAssignmentRepository(session)
        self.credentials = VPNCredentialRepository(session)
        self.grants = AccessGrantRepository(session)
        self.connections = ServerConnectionService(session)
        self.audit = AuditService(session, actor)
        self._prepared: set[UUID] = set()  # шлюзы, для которых install() в этой операции уже выполнен
        self.container_recreated = False  # install() пересоздал контейнер шлюза в рамках этой операции
        self.dropped = 0  # сколько назначений не попало в карту как негодные (см. build_map_checked)

    # --- соединение со шлюзом --------------------------------------------------------------------------------------

    @asynccontextmanager
    async def _provider(
        self, gateway: VPNProtocol, provider: AmneziaGatewayProvider | None
    ) -> AsyncIterator[AmneziaGatewayProvider]:
        """Готовый провайдер (sync уже держит соединение) или новое SSH-соединение со шлюзом."""
        if provider is not None:
            yield provider
            return
        ssh_client = await self.connections.create_ssh_client(gateway.server_id)
        built = self.connections.build_provider(ssh_client, ProtocolType.AMNEZIA_GATEWAY)
        assert isinstance(built, AmneziaGatewayProvider)
        async with ssh_client:
            yield built

    async def _gateway_of(self, credential: VPNCredential) -> VPNProtocol:
        grant = await self.grants.get_by_id(credential.access_grant_id)
        protocol = await self.protocols.get_by_id(grant.protocol_id) if grant else None
        if protocol is None or protocol.type != ProtocolType.AMNEZIA_GATEWAY:
            raise NotFoundError("Gateway protocol of the credential not found")
        return protocol

    # --- кандидаты и карта -----------------------------------------------------------------------------------------

    async def _candidates(
        self, gateway: VPNProtocol, provider: AmneziaGatewayProvider, *, skip: UUID | None = None
    ) -> dict[UUID, ExitCandidate]:
        """Выходы шлюза с текущей нагрузкой. Health берётся с самого шлюза сейчас (рукопожатие), а не из БД: сверка
        там бывает старой; шлюз не ответил - остаётся то, что сохранено."""
        exits = list(await self.exits.list_by_gateway(gateway.id))
        counts = await self.assignments.counts(gateway.id)
        statuses: list[ExitTunnelStatus] | None = None
        try:
            statuses = await provider.exit_status(expected=[row.slot for row in exits])
        except CommandExecutionError:
            logger.warning("exit tunnels not readable, using stored health")
        if statuses is not None:
            record_exit_status(exits, statuses, datetime.now(timezone.utc))
        return {
            row.id: ExitCandidate(
                exit_id=row.id,
                slot=row.slot,
                weight=row.weight,
                max_clients=row.max_clients,
                assigned=counts.get(row.id, 0),
                state=row.state,
                health=row.health,
            )
            for row in exits
            if row.id != skip
        }

    @staticmethod
    def _take(candidates: dict[UUID, ExitCandidate], chosen: ExitCandidate, delta: int) -> None:
        """Учесть в нагрузке выхода перенос клиента (без повторного запроса к БД)."""
        candidates[chosen.exit_id] = ExitCandidate(
            chosen.exit_id,
            chosen.slot,
            chosen.weight,
            chosen.max_clients,
            chosen.assigned + delta,
            chosen.state,
            chosen.health,
        )

    def _audit(self, credential_id: UUID, slot: int, reason: AssignReason) -> None:
        self.audit.record(
            AuditAction.GATEWAY_CLIENT_ASSIGNED,
            AuditEntity.VPN_CREDENTIAL,
            credential_id,
            {"credential_id": credential_id, "slot": slot, "reason": reason.value},
        )

    async def _ready(self, gateway: VPNProtocol, provider: AmneziaGatewayProvider) -> None:
        """Шлюз готов принять карту: идемпотентный `install()` дочиняет start.sh (цепочка и прыжки PREROUTING, защита
        FORWARD), правила и политику перезапуска и пересоздаёт контейнер, созданный с устаревшими sysctl. Само
        существование цепочки GW_ASSIGN готовности не доказывает: `iptables-restore` создаёт её и без прыжков.
        Пересоздание контейнера рвёт всех клиентов - оно пишется в аудит."""
        if gateway.id in self._prepared:
            return
        self._prepared.add(gateway.id)
        provider.recreated = None
        await provider.install()
        if provider.recreated:
            self.container_recreated = True
            self.audit.record(
                AuditAction.GATEWAY_CONTAINER_RECREATED,
                AuditEntity.SERVER,
                gateway.server_id,
                {"server_id": gateway.server_id, "reason": provider.recreated},
            )
            await self.session.commit()

    async def _apply_map(self, gateway: VPNProtocol, provider: AmneziaGatewayProvider) -> None:
        mapping, self.dropped = await self.assignments.build_map_checked(gateway.id)
        await provider.apply_assignments(mapping)

    # --- выдача и отзыв --------------------------------------------------------------------------------------------

    async def attach(self, credential: VPNCredential, provider: AmneziaGatewayProvider | None = None) -> int | None:
        """Назначает выход новому клиенту и применяет карту. Возвращает слот или None: исправных выходов нет - клиент
        остаётся без назначения (и без интернета), выданный конфиг не теряется; выход назначит `assign_unassigned`,
        когда появится исправный. Идемпотентно: уже назначенный клиент остаётся на своём выходе (карта применяется
        заново - так повтор задачи после сбоя применения чинит шлюз)."""
        gateway = await self._gateway_of(credential)
        async with self._provider(gateway, provider) as gw:
            await self._ready(gateway, gw)
            candidates = await self._candidates(gateway, gw)
            current = await self.assignments.get(credential.id)
            if current is not None and current.exit_id in candidates:
                await self.session.commit()
                await self._apply_map(gateway, gw)
                return candidates[current.exit_id].slot
            chosen = pick_exit(list(candidates.values()))
            if chosen is None:
                await self.session.commit()
                logger.warning("no healthy exits, client left without an exit", extra={"gateway": str(gateway.id)})
                return None
            await self.assignments.set(credential.id, chosen.exit_id, AssignReason.ISSUE)
            self._audit(credential.id, chosen.slot, AssignReason.ISSUE)
            await self.session.commit()
            await self._apply_map(gateway, gw)
            return chosen.slot

    async def detach(self, credential: VPNCredential, provider: AmneziaGatewayProvider | None = None) -> bool:
        """Снимает назначение отозванного клиента и пересобирает карту. False - назначения не было (шлюз не трогаем)."""
        assignment = await self.assignments.get(credential.id)
        if assignment is None:
            return False
        gateway = await self._gateway_of(credential)
        await self.assignments.delete(assignment)
        await self.session.commit()
        async with self._provider(gateway, provider) as gw:
            await self._ready(gateway, gw)
            await self._apply_map(gateway, gw)
        return True

    # --- массовые операции -----------------------------------------------------------------------------------------

    async def assign_unassigned(
        self,
        gateway: VPNProtocol,
        provider: AmneziaGatewayProvider | None = None,
        reason: AssignReason = AssignReason.ISSUE,
    ) -> int:
        """Назначает выходы всем живым клиентам шлюза без назначения (после добавления выхода, при сверке). Возвращает,
        скольким назначены; карта применяется один раз."""
        rows = await self.credentials.list_with_access_by_protocol_id(gateway.id)
        pending = [credential for credential, _, _ in rows if credential.revoked_at is None]
        pending = [credential for credential in pending if await self.assignments.get(credential.id) is None]
        if not pending:
            return 0
        async with self._provider(gateway, provider) as gw:
            await self._ready(gateway, gw)
            candidates = await self._candidates(gateway, gw)
            assigned = 0
            for credential in sorted(pending, key=lambda item: (item.created_at, item.id)):
                chosen = pick_exit(list(candidates.values()))
                if chosen is None:
                    break
                await self.assignments.set(credential.id, chosen.exit_id, reason)
                self._audit(credential.id, chosen.slot, reason)
                self._take(candidates, chosen, +1)
                assigned += 1
            await self.session.commit()
            if assigned:
                await self._apply_map(gateway, gw)
        return assigned

    async def drain(self, gateway_exit: GatewayExit, provider: AmneziaGatewayProvider | None = None) -> dict[str, int]:
        """Переносит клиентов снимаемого выхода на остальные (выбор - `pick_exit`, причина drain) и применяет карту.

        Порядок: изменения в БД -> flush -> применение карты на шлюзе -> commit. Применение упало - ничего не
        сохранено (воркер откатывает сессию), повтор пересчитывает тот же перенос; commit упал после применения -
        повтор применит ту же карту. Карта применяется ВСЕГДА, даже если переносить некого (повтор после сбоя не
        оставит на шлюзе правил с меткой удалённого выхода). Отозванные credential'ы сначала убираются, чтобы не
        искажать нагрузку. Нет подходящих выходов - назначения удаляются (kill-switch), по каждому клиенту событие
        `gateway_client_unassigned`. Счётчики копятся в строке выхода и переживают повторы. Возвращает итог.
        """
        gateway = await self.protocols.get_by_id(gateway_exit.gateway_protocol_id)
        if gateway is None:
            raise NotFoundError("Gateway protocol not found")
        async with self._provider(gateway, provider) as gw:
            await self._ready(gateway, gw)
            await self.assignments.delete_dead(gateway.id)
            moving = list(await self.assignments.list_for_exit(gateway_exit.id))
            candidates = await self._candidates(gateway, gw, skip=gateway_exit.id)
            moved = unassigned = 0
            for assignment in moving:
                chosen = pick_exit(list(candidates.values()))
                if chosen is None:
                    self.audit.record(
                        AuditAction.GATEWAY_CLIENT_UNASSIGNED,
                        AuditEntity.VPN_CREDENTIAL,
                        assignment.credential_id,
                        {"credential_id": assignment.credential_id, "slot": gateway_exit.slot},
                    )
                    await self.assignments.delete(assignment)
                    unassigned += 1
                    continue
                await self.assignments.set(assignment.credential_id, chosen.exit_id, AssignReason.DRAIN)
                self._audit(assignment.credential_id, chosen.slot, AssignReason.DRAIN)
                self._take(candidates, chosen, +1)
                moved += 1
            gateway_exit.drained_moved += moved
            gateway_exit.drained_unassigned += unassigned
            await self.session.flush()
            await self._apply_map(gateway, gw)
            await self.session.commit()
        return {"moved": gateway_exit.drained_moved, "unassigned": gateway_exit.drained_unassigned}

    async def estimate_unassigned(self, gateway_exit: GatewayExit) -> int:
        """Сколько клиентов этого выхода останется без выхода после переноса. Только БД (сохранённый health), без SSH:
        `pick_exit` на копии состояния. Вызывается при постановке снятия, до любых изменений."""
        exits = await self.exits.list_by_gateway(gateway_exit.gateway_protocol_id)
        counts = await self.assignments.counts(gateway_exit.gateway_protocol_id)
        candidates = {
            row.id: ExitCandidate(
                row.id, row.slot, row.weight, row.max_clients, counts.get(row.id, 0), row.state, row.health
            )
            for row in exits
            if row.id != gateway_exit.id
        }
        left = 0
        for _ in range(counts.get(gateway_exit.id, 0)):
            chosen = pick_exit(list(candidates.values()))
            if chosen is None:
                left += 1
            else:
                self._take(candidates, chosen, +1)
        return left

    # --- сверка (sync шлюза) ---------------------------------------------------------------------------------------

    async def reconcile(
        self, gateway: VPNProtocol, provider: AmneziaGatewayProvider, issues: list[str]
    ) -> dict[str, Any]:
        """Сверка шлюза с БД. Меняет только то, что отстало от БД (БД - источник правды): туннели выходов с устаревшим
        конфигом пересоздаются, клиенты без назначения получают выход, карта на шлюзе, не совпавшая с БД, применяется
        заново. Отчёт и расхождения (issues) - без адресов и ключей."""
        report: dict[str, Any] = {"assigned": 0, "newly_assigned": 0, "reapplied": False}
        await self._ready(gateway, provider)  # start.sh, правила, sysctl контейнера - до любых проверок карты
        report["container_recreated"] = self.container_recreated
        if self.container_recreated:
            issues.append("The gateway container was recreated (outdated sysctl): all clients reconnect")
        await self.assignments.delete_dead(gateway.id)
        await self.session.commit()
        await self._refresh_tunnels(gateway, provider, issues)
        try:
            report["newly_assigned"] = await self.assign_unassigned(gateway, provider, AssignReason.SYNC)
        except CommandExecutionError as exc:
            issues.append(f"Failed to assign exits to new clients: {exc}")
        desired, dropped = await self.assignments.build_map_checked(gateway.id)
        report["assigned"] = len(desired)
        if dropped:
            report["skipped"] = dropped
            issues.append(f"{dropped} client assignment(s) were skipped: invalid or duplicate address")
        try:
            in_sync = await provider.assignments_in_sync(desired)
        except CommandExecutionError:
            in_sync = False  # цепочки нет даже после install(): применение создаст её (прыжки проверит провайдер)
        if not in_sync:
            await provider.apply_assignments(desired)
            report["reapplied"] = True
            issues.append("Client assignments on the gateway differed from the DB and were re-applied")
        return report

    async def _refresh_tunnels(self, gateway: VPNProtocol, provider: AmneziaGatewayProvider, issues: list[str]) -> None:
        """Приводит ПОДНЯТЫЕ туннели ACTIVE-выходов к конфигу из БД (идемпотентно): выход, поднятый конфигом
        прежней версии (без правила метки), пересоздаётся; сменившийся адрес сервера-выхода применяется через
        syncconf. Упавший туннель не поднимается (при отказе автодействий нет - failover этапа 24.5): о нём сообщает
        отчёт по выходам."""
        try:
            up = {status.slot for status in await provider.exit_status() if status.interface_up}
        except CommandExecutionError:
            return
        tunnels = []
        for row in await self.exits.list_by_gateway(gateway.id):
            if row.state == ExitState.ACTIVE and row.slot in up:
                try:
                    tunnels.append(await exit_tunnel(self.session, row))
                except NotFoundError:
                    issues.append(f"Exit slot {row.slot} has no stored tunnel config")
        if not tunnels:
            return
        try:
            await provider.apply_exits(tunnels)
        except CommandExecutionError as exc:
            issues.append(f"Failed to refresh exit tunnels: {exc}")
