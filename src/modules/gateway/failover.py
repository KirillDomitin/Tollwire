#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.exceptions import (
    CredentialNotFound,
    GatewayExitError,
    InvalidDataError,
    JobAlreadyRunningError,
    NotFoundError,
    StateConflictError,
)
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import exit_health_label
from infrastructure.workers.locks import ServerLock
from modules.access.repository import AccessGrantRepository, VPNCredentialRepository
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.gateway.assignment import GatewayAssignmentService
from modules.gateway.balancer import (
    BAD_HEALTH,
    FAILURE_THRESHOLD,
    UNUSABLE_HEALTH,
    AssignmentView,
    ExitView,
    FailoverPlan,
    next_health,
    plan_failover,
)
from modules.gateway.exit_health import record_exit_status
from modules.gateway.models.gateway_assignment import AssignReason, GatewayAssignment
from modules.gateway.models.gateway_exit import ExitState, GatewayExit
from modules.gateway.repository import GatewayAssignmentRepository, GatewayExitRepository
from modules.gateway.schemas import GatewayAssignmentRead
from modules.jobs.models.job import Job, JobType
from modules.jobs.repository import JobRepository
from modules.jobs.service import JobService
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import CredentialRepository, ServerRepository

logger = logging.getLogger("gateway.failover")


@dataclass
class WatchResult:
    """Итог проверки шлюза правилом `gateway_watch`."""

    checked: bool = False  # шлюз опрошен (есть ACTIVE-выходы)
    failed: list[int] = field(default_factory=list)  # слоты, ставшие отказавшими в этой проверке
    recovered: list[int] = field(default_factory=list)  # слоты, восстановившиеся в этой проверке
    all_down: bool = False  # плохи ВСЕ ACTIVE-выходы: проблема шлюза или тотальный отказ, счётчики не растут
    pending_moves: int = 0  # сколько клиентов план перенёс бы сейчас
    queued: bool = False  # поставлена задача GATEWAY_APPLY


class GatewayFailoverService:
    """Failover выходов шлюза и ручной перенос / закрепление клиентов (этап 24.5).

    Разделение труда: правило планировщика `gateway_watch` только ЧИТАЕТ состояние туннелей (SSH к шлюзу, без замка),
    обновляет счётчики выходов и, если пора что-то менять, ставит задачу `GATEWAY_APPLY`.
    Саму задачу воркер выполняет под ServerLock шлюза и ПЕРЕСЧИТЫВАЕТ решение по свежим данным БД (`plan_failover`),
    а не берёт его из правила; карта назначений применяется на шлюзе один раз. Ручной перенос идёт тем же путём под
    тем же замком (прямо из запроса).
    """

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.protocols = ProtocolRepository(session)
        self.servers = ServerRepository(session)
        self.exits = GatewayExitRepository(session)
        self.assignments = GatewayAssignmentRepository(session)
        self.credentials = VPNCredentialRepository(session)
        self.grants = AccessGrantRepository(session)
        self.audit = AuditService(session, actor)
        self.assign = GatewayAssignmentService(session, actor)

    # --- проверка (правило планировщика) --------------------------------------------------------------------------

    async def active_exits(self, gateway: VPNProtocol) -> list[GatewayExit]:
        return [row for row in await self.exits.list_by_gateway(gateway.id) if row.state == ExitState.ACTIVE]

    async def check(
        self, gateway: VPNProtocol, provider: AmneziaGatewayProvider, *, authoritative: bool = True
    ) -> WatchResult:
        """Одна проверка всех ACTIVE-выходов: читает туннели, обновляет health / счётчики / `healthy_since`.

        Хорошая проверка решается ТОЛЬКО по туннелю: интерфейс поднят и рукопожатие свежее (окно
        `ACTIVE_HANDSHAKE_WINDOW_SECONDS`); состояние сервера-выхода (`health_status`) обновляется редко и не зависит от
        туннеля, поэтому на решение не влияет. Выход отказал после `FAILURE_THRESHOLD` плохих проверок подряд.

        - Плохи ВСЕ ACTIVE-выходы сразу (перезапуск контейнера шлюза, сбой самого шлюза, тотальный отказ): переносить
          некуда, счётчики НЕ растут (иначе первый поднявшийся выход получил бы всех клиентов), health помечается,
          рукопожатие
          не трогается; переход «были исправные -> все плохи» пишется один раз (`gateway_all_exits_down`, по прежнему
          health выходов), возврат - `gateway_exit_recovered`.
        - `authoritative=False` (ручная проверка из задачи): счётчик ведёт планировщик, поэтому ручная проверка не
          увеличивает его, если с прошлой проверки выхода прошло меньше половины интервала `gateway_watch` (иначе
          «две подряд» превращались бы в две пробы с интервалом в секунды); health и рукопожатие обновляются всегда.
        Ошибка чтения туннелей (CommandExecutionError) уходит вызывающему: счётчики не трогаются."""
        result = WatchResult()
        active = await self.active_exits(gateway)
        if not active:
            return result
        statuses = await provider.exit_status(expected=[row.slot for row in active])
        now = datetime.now(timezone.utc)
        by_slot = {status.slot: status for status in statuses}
        labels = {row.id: exit_health_label(by_slot.get(row.slot), now) for row in active}
        good = {row_id: label == "ok" for row_id, label in labels.items()}
        was_all_bad = all(row.health in BAD_HEALTH for row in active)
        previous_check = {row.id: row.checked_at for row in active}
        result.checked = True
        if not any(good.values()):
            for row in active:
                row.health, row.checked_at, row.healthy_since = labels[row.id], now, None
            result.all_down = True
            if not was_all_bad:
                self.audit.record(
                    AuditAction.GATEWAY_ALL_EXITS_DOWN,
                    AuditEntity.SERVER,
                    gateway.server_id,
                    {"server_id": gateway.server_id, "exits": len(active)},
                )
            await self.session.commit()
            return result
        record_exit_status(active, statuses, now)
        half_interval = timedelta(seconds=settings.gateway_watch_interval_seconds / 2)
        for row in active:
            earlier = previous_check[row.id]
            if not authoritative and earlier is not None and now - earlier < half_interval:
                continue  # счётчик ведёт планировщик
            step = next_health(row.consecutive_failures, row.healthy_since, good[row.id], now)
            row.consecutive_failures, row.healthy_since = step.consecutive_failures, step.healthy_since
            if step.event == "failed":
                result.failed.append(row.slot)
            if step.event == "recovered" or (was_all_bad and good[row.id]):
                row.failure_notified = False
                result.recovered.append(row.slot)
                self.audit.record(
                    AuditAction.GATEWAY_EXIT_RECOVERED,
                    AuditEntity.SERVER,
                    gateway.server_id,
                    {"exit_id": row.id, "slot": row.slot},
                )
        await self.session.commit()
        return result

    async def watch(self, gateway: VPNProtocol) -> WatchResult:
        """Проверка шлюза для правила: SSH на чтение, счётчики и - если выход отказал / восстановился или план не пуст -
        задача GATEWAY_APPLY (без дублей)."""
        if not await self.active_exits(gateway):
            return WatchResult()
        ssh_client = await self.assign.connections.create_ssh_client(gateway.server_id)
        built = self.assign.connections.build_provider(ssh_client, ProtocolType.AMNEZIA_GATEWAY)
        assert isinstance(built, AmneziaGatewayProvider)
        async with ssh_client:
            result = await self.check(gateway, built)
        plan = await self.plan(gateway)
        result.pending_moves = len(plan.moves)
        if result.failed or result.recovered or plan.moves:
            result.queued = await self._enqueue(gateway.server_id, {})
        return result

    async def _enqueue(self, server_id: UUID, params: dict[str, Any]) -> bool:
        if await JobRepository(self.session).has_unfinished_for_server(JobType.GATEWAY_APPLY, server_id):
            return False
        await JobService(self.session, self.actor).create(JobType.GATEWAY_APPLY, server_id, params=params or None)
        return True

    # --- план -------------------------------------------------------------------------------------------------------

    async def plan(self, gateway: VPNProtocol, now: datetime | None = None) -> FailoverPlan:
        """План переносов по текущему состоянию БД (SSH не нужен)."""
        plan, _ = await self._plan_with_exits(gateway, now)
        return plan

    async def _plan_with_exits(
        self, gateway: VPNProtocol, now: datetime | None = None
    ) -> tuple[FailoverPlan, dict[UUID, GatewayExit]]:
        now = now or datetime.now(timezone.utc)
        exits = await self.active_exits(gateway)
        views = [
            ExitView(
                row.id,
                row.slot,
                row.weight,
                row.max_clients,
                failed=row.consecutive_failures >= FAILURE_THRESHOLD,
                # то же правило, что `GatewayAssignmentService._candidates` для новых клиентов: плохая проверка или
                # health down / stale - выход цели переноса не принимает
                usable=row.consecutive_failures == 0 and row.health not in UNUSABLE_HEALTH,
                healthy_since=row.healthy_since,
            )
            for row in exits
        ]
        live = list(await self.assignments.list_live(gateway.id))
        plan = plan_failover(
            views,
            [AssignmentView(item.credential_id, item.exit_id) for item in live],
            {item.credential_id: item.pinned_exit_id for item in live if item.pinned_exit_id is not None},
            now,
            timedelta(seconds=settings.gateway_failover_return_after_seconds),
        )
        return plan, {row.id: row for row in exits}

    # --- применение (воркер, под ServerLock шлюза) -----------------------------------------------------------------

    async def run_apply(self, job: Job) -> dict[str, Any]:
        """GATEWAY_APPLY: при `params.check` сначала проверка (кнопка «проверить сейчас»), затем план по свежим данным и
        применение карты на шлюзе одним разом. Пустой план - шлюз не трогаем. Порядок: БД -> flush -> карта -> commit:
        карта не применилась - ничего не сохранено, следующая проверка планирует то же снова."""
        if job.server_id is None:
            raise InvalidDataError("Gateway job has no server")
        gateway = await self.protocols.get_by_server_and_type(job.server_id, ProtocolType.AMNEZIA_GATEWAY)
        if gateway is None:
            raise GatewayExitError("The gateway is not installed on this server")
        if (job.params or {}).get("check"):
            ssh_client = await self.assign.connections.create_ssh_client(gateway.server_id)
            built = self.assign.connections.build_provider(ssh_client, ProtocolType.AMNEZIA_GATEWAY)
            assert isinstance(built, AmneziaGatewayProvider)
            async with ssh_client:
                await self.check(gateway, built, authoritative=False)
        plan, exits = await self._plan_with_exits(gateway)
        report: dict[str, Any] = {"moved": 0, "returned": 0, "stranded": sum(plan.stranded.values())}
        events = self._failover_events(plan, exits)
        if not plan.moves and not events:
            return report
        if plan.moves:
            async with self.assign._provider(gateway, None) as provider:
                await self.assign._ready(gateway, provider)
                await self._apply_moves(gateway, plan, exits)
                await self.assign._apply_map(gateway, provider)
                self._record_events(gateway, events)
                await self.session.commit()
        else:
            self._record_events(gateway, events)
            await self.session.commit()
        report["moved"] = sum(1 for move in plan.moves if move.reason == AssignReason.FAILOVER.value)
        report["returned"] = len(plan.moves) - report["moved"]
        return report

    def _failover_events(
        self, plan: FailoverPlan, exits: dict[UUID, GatewayExit]
    ) -> list[tuple[GatewayExit, int, int]]:
        """События `gateway_failover` (выход, перенесено, без выхода). Пишутся, когда с отказавшего выхода кого-то
        перенесли, а «некуда деть» - один раз за отказ: признак `failure_notified` в строке выхода (сбрасывается при
        восстановлении) не зависит от того, какая именно задача и в какой момент отработала."""
        moved: dict[UUID, int] = {}
        for move in plan.moves:
            if move.reason == AssignReason.FAILOVER.value:
                moved[move.from_exit] = moved.get(move.from_exit, 0) + 1
        events = []
        for exit_id in sorted({*moved, *plan.stranded}, key=lambda value: exits[value].slot):
            row = exits[exit_id]
            stranded = plan.stranded.get(exit_id, 0)
            if moved.get(exit_id) or (stranded and not row.failure_notified):
                events.append((row, moved.get(exit_id, 0), stranded))
        return events

    def _record_events(self, gateway: VPNProtocol, events: list[tuple[GatewayExit, int, int]]) -> None:
        for row, moved, stranded in events:
            row.failure_notified = True
            self.audit.record(
                AuditAction.GATEWAY_FAILOVER,
                AuditEntity.SERVER,
                gateway.server_id,
                {"exit_id": row.id, "slot": row.slot, "moved": moved, "unassigned": stranded},
            )

    async def _apply_moves(self, gateway: VPNProtocol, plan: FailoverPlan, exits: dict[UUID, GatewayExit]) -> None:
        for move in plan.moves:
            reason = AssignReason(move.reason)
            await self.assignments.set(move.credential_id, move.to_exit, reason)
            self._audit_moved(move.credential_id, exits[move.from_exit].slot, exits[move.to_exit].slot, reason.value)
        await self.session.flush()

    def _audit_moved(self, credential_id: UUID, from_slot: int | None, to_slot: int, reason: str) -> None:
        self.audit.record(
            AuditAction.GATEWAY_CLIENT_MOVED,
            AuditEntity.VPN_CREDENTIAL,
            credential_id,
            {"credential_id": credential_id, "from_slot": from_slot, "to_slot": to_slot, "reason": reason},
        )

    # --- ручное управление ------------------------------------------------------------------------------------------

    async def _gateway(self, server_id: UUID) -> VPNProtocol:
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError("Server not found")
        protocol = await self.protocols.get_by_server_and_type(server_id, ProtocolType.AMNEZIA_GATEWAY)
        if protocol is None:
            raise GatewayExitError("The gateway is not installed on this server")
        return protocol

    async def schedule_check(self, server_id: UUID) -> Job:
        """POST .../gateway/check: «проверить сейчас» - задача GATEWAY_APPLY с проверкой (202). 404 нет сервера, 409 нет
        шлюза / SSH-учётки / такая задача уже идёт."""
        gateway = await self._gateway(server_id)
        if not await CredentialRepository(self.session).get_by_server_id(server_id):
            raise CredentialNotFound("SSH credentials are not set for this server")
        if await JobRepository(self.session).has_unfinished_for_server(JobType.GATEWAY_APPLY, gateway.server_id):
            raise JobAlreadyRunningError("The gateway is already being checked")
        return await JobService(self.session, self.actor).create(
            JobType.GATEWAY_APPLY, gateway.server_id, params={"check": True}
        )

    async def move_client(
        self, server_id: UUID, credential_id: UUID, exit_id: UUID | None, pin: bool
    ) -> GatewayAssignmentRead:
        """PATCH .../gateway/assignments/{credential_id}: перенос клиента на ACTIVE-выход шлюза (причина manual) и /
        или закрепление (`pin=True` - за целевым выходом, без `exit_id` - за текущим; `pin=False` снимает закрепление).
        Ручной перенос - осознанное решение админа: `max_clients` и счётчик отказов не проверяются, только что выход
        ACTIVE этого шлюза и не отказавший. Применяется синхронно под ServerLock шлюза (простое прямое решение:
        ответ сразу несёт итоговое назначение).
        Ошибки: 404 - нет сервера / клиента этого шлюза / выхода этого шлюза; 409 - нет шлюза, клиент отозван или без
        выхода, выход не ACTIVE или отказал, шлюз занят."""
        gateway = await self._gateway(server_id)
        async with ServerLock(gateway.server_id):
            credential = await self.credentials.get_by_id(credential_id)
            grant = await self.grants.get_by_id(credential.access_grant_id) if credential else None
            if credential is None or grant is None or grant.protocol_id != gateway.id:
                raise NotFoundError("Client not found on this gateway")
            if credential.revoked_at is not None:
                raise StateConflictError("The client is revoked")
            current = await self.assignments.get(credential_id)
            target: GatewayExit | None = None
            if exit_id is not None:
                target = await self.exits.get_for_gateway(gateway.id, exit_id)
                if target is None:
                    raise NotFoundError("Exit not found")
                if target.state != ExitState.ACTIVE or target.consecutive_failures >= FAILURE_THRESHOLD:
                    raise GatewayExitError("The exit is not available: only a working active exit can take clients")
            elif pin:
                if current is None:
                    raise StateConflictError("The client has no exit to pin")
                target = await self.exits.get_by_id(current.exit_id)
            if target is None and current is None:
                raise StateConflictError("The client has no assignment")
            slots = {row.id: row.slot for row in await self.exits.list_by_gateway(gateway.id)}
            from_slot = slots.get(current.exit_id) if current is not None else None
            moved = target is not None and (current is None or current.exit_id != target.id)
            if moved:
                # Порядок «готовность шлюза -> БД -> карта -> commit», как в run_apply: `_ready` при пересоздании
                # контейнера коммитит, поэтому он идёт ДО любых изменений назначений.
                async with self.assign._provider(gateway, None) as provider:
                    await self.assign._ready(gateway, provider)
                    assignment = await self._change(credential_id, target, pin, from_slot, slots, moved=True)
                    await self.assign._apply_map(gateway, provider)
            else:
                assignment = await self._change(credential_id, target, pin, from_slot, slots, moved=False)
            await self.session.commit()
            await self.session.refresh(assignment)
            return self._read(assignment, slots)

    async def _change(
        self,
        credential_id: UUID,
        target: GatewayExit | None,
        pin: bool,
        from_slot: int | None,
        slots: dict[UUID, int],
        *,
        moved: bool,
    ) -> GatewayAssignment:
        """Изменения назначения в БД (flush без commit): перенос и / или закрепление."""
        if moved:
            assert target is not None
            await self.assignments.set(credential_id, target.id, AssignReason.MANUAL)
            self._audit_moved(credential_id, from_slot, target.slot, AssignReason.MANUAL.value)
        assignment = await self.assignments.get(credential_id)
        assert assignment is not None
        new_pin = target.id if (pin and target is not None) else None
        if assignment.pinned_exit_id != new_pin:
            assignment.pinned_exit_id = new_pin
            if not moved:
                slot = slots.get(assignment.exit_id, 0)
                self._audit_moved(credential_id, slot, slot, "pin" if new_pin else "unpin")
        await self.session.flush()
        return assignment

    @staticmethod
    def _read(assignment: GatewayAssignment, slots: dict[UUID, int]) -> GatewayAssignmentRead:
        return GatewayAssignmentRead(
            credential_id=assignment.credential_id,
            exit_id=assignment.exit_id,
            slot=slots.get(assignment.exit_id, 0),
            reason=assignment.reason,
            assigned_at=assignment.assigned_at,
            pinned_exit_id=assignment.pinned_exit_id,
        )
