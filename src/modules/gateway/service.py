#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import (
    CredentialNotFound,
    GatewayExitError,
    InvalidDataError,
    JobAlreadyRunningError,
    NotFoundError,
    SSHError,
)
from core.security import encrypt_secret
from core.ssh_exception import CommandExecutionError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.workers.locks import ServerLock
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService, diff_changes
from modules.gateway.assignment import GatewayAssignmentService
from modules.gateway.models.gateway_exit import ExitState, GatewayExit
from modules.gateway.naming import exit_client_name
from modules.gateway.repository import GatewayAssignmentRepository, GatewayExitRepository
from modules.gateway.schemas import GatewayAssignmentRead, GatewayExitCreate, GatewayExitRead, GatewayExitUpdate
from modules.gateway.tunnels import exit_tunnel
from modules.jobs.models.job import Job, JobType
from modules.jobs.service import JobService
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.servers.models.server import VPNServer
from modules.servers.repository import CredentialRepository, ServerRepository
from modules.servers.service import ServerConnectionService

logger = logging.getLogger("gateway.exits")

# Состояния, в которых добавление можно повторить: на шлюзе туннеля ещё нет, фазы идемпотентны.
_RETRYABLE_STATES = (ExitState.PENDING, ExitState.PEER_CREATED)


class GatewayExitService:
    """Выходы шлюза: зарубежные серверы панели, к которым шлюз подключён как клиент (интерфейс gwx<слот> на шлюзе).

    API-часть (`schedule_*`, `update`, `list_exits`) работает только с БД и ставит задачу; `run_add` / `run_remove`
    выполняет воркер под ServerLock шлюза. Выход блокируется ВНУТРИ, всегда после шлюза (порядок шлюз -> выход):
    так две задачи не ждут друг друга по кругу. Занятый выход (ServerBusyError) воркер возвращает в очередь.
    """

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.servers = ServerRepository(session)
        self.server_credentials = CredentialRepository(session)
        self.protocols = ProtocolRepository(session)
        self.exits = GatewayExitRepository(session)
        self.assignments = GatewayAssignmentRepository(session)
        self.connections = ServerConnectionService(session)
        self.audit = AuditService(session, actor)

    # --- API: только БД ----------------------------------------------------------------------------------------

    async def _gateway(self, server_id: UUID) -> VPNProtocol:
        """Протокол шлюза сервера. Нет сервера - NotFoundError, нет шлюза - GatewayExitError (409)."""
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError("Server not found")
        protocol = await self.protocols.get_by_server_and_type(server_id, ProtocolType.AMNEZIA_GATEWAY)
        if protocol is None:
            raise GatewayExitError("The gateway is not installed on this server")
        return protocol

    async def _exit_of(self, gateway: VPNProtocol, exit_id: UUID) -> GatewayExit:
        gateway_exit = await self.exits.get_for_gateway(gateway.id, exit_id)
        if gateway_exit is None:
            raise NotFoundError("Exit not found")
        return gateway_exit

    async def _view(self, gateway_exit: GatewayExit) -> GatewayExitRead:
        protocol = await self.protocols.get_by_id(gateway_exit.exit_protocol_id)
        server = await self.servers.get_by_id(protocol.server_id) if protocol else None
        if protocol is None or server is None:  # FK не допускает; страховка от порчи данных
            raise NotFoundError("Exit server not found")
        assigned = (await self.assignments.counts(gateway_exit.gateway_protocol_id)).get(gateway_exit.id, 0)
        return self._read(gateway_exit, server.id, server.name, assigned)

    @staticmethod
    def _read(gateway_exit: GatewayExit, server_id: UUID, server_name: str, assigned: int) -> GatewayExitRead:
        return GatewayExitRead(
            id=gateway_exit.id,
            exit_server_id=server_id,
            exit_server_name=server_name,
            slot=gateway_exit.slot,
            state=gateway_exit.state,
            health=gateway_exit.health,
            last_handshake_at=gateway_exit.last_handshake_at,
            checked_at=gateway_exit.checked_at,
            weight=gateway_exit.weight,
            max_clients=gateway_exit.max_clients,
            assigned_clients=assigned,
        )

    async def list_exits(self, server_id: UUID) -> list[GatewayExitRead]:
        """Выходы шлюза по слотам. Сервер без шлюза - пустой список (404 только если сервера нет)."""
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError("Server not found")
        gateway = await self.protocols.get_by_server_and_type(server_id, ProtocolType.AMNEZIA_GATEWAY)
        if gateway is None:
            return []
        rows = await self.exits.list_with_server_names(gateway.id)
        counts = await self.assignments.counts(gateway.id)
        return [
            self._read(gateway_exit, exit_server_id, name, counts.get(gateway_exit.id, 0))
            for gateway_exit, exit_server_id, name in rows
        ]

    async def list_assignments(self, server_id: UUID) -> list[GatewayAssignmentRead]:
        """Живые назначения шлюза: клиент (credential), выход, слот, причина, время. Адресов и ключей клиентов нет."""
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError("Server not found")
        gateway = await self.protocols.get_by_server_and_type(server_id, ProtocolType.AMNEZIA_GATEWAY)
        if gateway is None:
            return []
        return [
            GatewayAssignmentRead(
                credential_id=assignment.credential_id,
                exit_id=assignment.exit_id,
                slot=slot,
                reason=assignment.reason,
                assigned_at=assignment.assigned_at,
            )
            for assignment, slot in await self.assignments.list_for_gateway(gateway.id)
        ]

    async def _exit_protocol(self, gateway_server_id: UUID, data: GatewayExitCreate) -> tuple[VPNServer, VPNProtocol]:
        """Сервер-выход и его протокол AMNEZIA_WG. Всё, что в выходы не годится, - понятная ошибка до постановки."""
        if data.exit_server_id == gateway_server_id:
            raise InvalidDataError("A server cannot be an exit of itself")
        exit_server = await self.servers.get_by_id(data.exit_server_id)
        if exit_server is None:
            raise NotFoundError("Exit server not found")
        if not await self.server_credentials.get_by_server_id(exit_server.id):
            raise CredentialNotFound("SSH credentials are not set for the exit server")
        if await self.protocols.get_by_server_and_type(exit_server.id, ProtocolType.AMNEZIA_GATEWAY):
            raise GatewayExitError("A gateway cannot be an exit of another gateway")
        protocol = await self.protocols.get_by_server_and_type(exit_server.id, ProtocolType.AMNEZIA_WG)
        if protocol is None:
            raise GatewayExitError("The AmneziaWG protocol is not installed on the exit server")
        if not protocol.port:
            raise GatewayExitError("The AmneziaWG port of the exit server is unknown, run sync first")
        return exit_server, protocol

    async def schedule_add(self, server_id: UUID, data: GatewayExitCreate) -> Job:
        """Подключить сервер выходом шлюза: строка PENDING и задача GATEWAY_ADD_EXIT (202). Быстрые проверки по БД, SSH
        не открывается. Выход, который уже добавляется (PENDING / PEER_CREATED, задача упала), можно поставить
        повторно: тот же слот, фазы идемпотентны. Добавленный (ACTIVE и дальше) - 409."""
        gateway = await self._gateway(server_id)
        if not await self.server_credentials.get_by_server_id(server_id):
            raise CredentialNotFound("SSH credentials are not set for this server")
        exit_server, exit_protocol = await self._exit_protocol(server_id, data)

        await self.exits.lock_gateway(gateway.id)  # два одновременных добавления не возьмут один слот
        gateway_exit = await self.exits.get_by_pair(gateway.id, exit_protocol.id)
        if gateway_exit is not None:
            if await self.exits.has_unfinished_job(gateway_exit.id):
                raise JobAlreadyRunningError("This exit is already being added or removed")
            if gateway_exit.state not in _RETRYABLE_STATES:
                raise GatewayExitError("This server is already an exit of the gateway")
            gateway_exit.weight, gateway_exit.max_clients = data.weight, data.max_clients
        else:
            slot = await self.exits.free_slot(gateway.id)
            if slot is None:
                raise GatewayExitError("No free exit slots left on the gateway")
            gateway_exit = await self.exits.add(
                GatewayExit(
                    gateway_protocol_id=gateway.id,
                    exit_protocol_id=exit_protocol.id,
                    slot=slot,
                    weight=data.weight,
                    max_clients=data.max_clients,
                    state=ExitState.PENDING,
                )
            )
        params = {
            "exit_id": str(gateway_exit.id),
            "exit_server_id": str(exit_server.id),
            "slot": gateway_exit.slot,
            "weight": gateway_exit.weight,
        }
        # JobService.create коммитит вместе со строкой выхода.
        return await JobService(self.session, self.actor).create(JobType.GATEWAY_ADD_EXIT, server_id, params=params)

    async def schedule_remove(self, server_id: UUID, exit_id: UUID, force: bool = False) -> Job:
        """Снять выход: состояние DRAINING (новых клиентов на него не назначать) и задача GATEWAY_REMOVE_EXIT (202).
        Подходит любое состояние: незаконченное добавление тоже снимается. Повтор после сбоя задачи - тот же вызов.

        Клиенты, которым после переноса не хватит выхода (последний выход или нет места по `max_clients`), остались бы
        без интернета: без `force` это 409 с числом таких клиентов (оценка по БД, `pick_exit` на копии состояния);
        с `force` задача идёт, а каждый оставшийся без выхода клиент пишется в аудит."""
        gateway = await self._gateway(server_id)
        if not await self.server_credentials.get_by_server_id(server_id):
            raise CredentialNotFound("SSH credentials are not set for this server")
        await self.exits.lock_gateway(gateway.id)  # как у schedule_add: POST и DELETE одного выхода не идут вперемешку
        gateway_exit = await self._exit_of(gateway, exit_id)
        if await self.exits.has_unfinished_job(gateway_exit.id):
            raise JobAlreadyRunningError("This exit is already being added or removed")
        left = await GatewayAssignmentService(self.session, self.actor).estimate_unassigned(gateway_exit)
        if left and not force:
            raise GatewayExitError(
                f"{left} client(s) of this exit would be left without an exit and without internet; "
                "add another exit first or confirm with ?force=true"
            )
        protocol = await self.protocols.get_by_id(gateway_exit.exit_protocol_id)
        gateway_exit.state = ExitState.DRAINING if gateway_exit.state != ExitState.REMOVING else ExitState.REMOVING
        params = {
            "exit_id": str(gateway_exit.id),
            "exit_server_id": str(protocol.server_id) if protocol else None,
            "slot": gateway_exit.slot,
            "force": force,
        }
        return await JobService(self.session, self.actor).create(JobType.GATEWAY_REMOVE_EXIT, server_id, params=params)

    async def update(self, server_id: UUID, exit_id: UUID, data: GatewayExitUpdate) -> GatewayExitRead:
        """Вес и предел клиентов - только БД, без задач. В запросе переданы поля, которые меняем; `max_clients: null`
        снимает предел, `weight: null` недопустим (422)."""
        gateway = await self._gateway(server_id)
        gateway_exit = await self._exit_of(gateway, exit_id)
        fields = data.model_dump(exclude_unset=True)
        if "weight" in fields and fields["weight"] is None:
            raise InvalidDataError("Weight cannot be null")
        changes = diff_changes(gateway_exit, fields)
        for name, value in fields.items():
            setattr(gateway_exit, name, value)
        view = await self._view(gateway_exit)
        if changes:
            self.audit.record(
                AuditAction.GATEWAY_EXIT_UPDATED,
                AuditEntity.SERVER,
                server_id,
                {
                    "exit_id": gateway_exit.id,
                    "exit_server_id": view.exit_server_id,
                    "slot": gateway_exit.slot,
                    "changes": changes,
                },
            )
        await self.session.commit()
        return view

    # --- воркер: под ServerLock шлюза --------------------------------------------------------------------------

    def _exit_id(self, job: Job) -> UUID:
        try:
            return UUID(str((job.params or {})["exit_id"]))
        except (KeyError, ValueError):
            raise InvalidDataError("Gateway exit job has invalid params") from None

    async def run_add(self, job: Job) -> dict[str, Any]:
        """Добавление выхода в две фазы с сохранённым состоянием; повтор идемпотентен.

        Фаза 1 (PENDING): внутренний клиент `gw-<id>` на выходе под ServerLock выхода; ключ, адрес и зашифрованный
        конфиг сохранены, PEER_CREATED, commit. Фаза 2 (PEER_CREATED): туннель на шлюзе (`apply_exits`), ACTIVE.
        Сбой между фазами не создаёт второго клиента: PEER_CREATED повторяет только фазу 2, а клиент, чей commit не
        дошёл (в строке нет ключа), находится на выходе по имени и пересоздаётся.
        """
        if job.server_id is None:
            raise InvalidDataError("Gateway exit job has no server")
        gateway = await self._gateway(job.server_id)
        gateway_exit = await self._exit_of(gateway, self._exit_id(job))
        if gateway_exit.state not in _RETRYABLE_STATES:
            raise GatewayExitError("The exit is not being added")
        exit_protocol = await self.protocols.get_by_id(gateway_exit.exit_protocol_id)
        if exit_protocol is None:
            raise NotFoundError("Exit protocol not found")
        slot = gateway_exit.slot
        if gateway_exit.state == ExitState.PENDING:
            await self._create_peer(gateway_exit, exit_protocol.server_id)
        await self._activate(gateway_exit, job.server_id, exit_protocol)
        result: dict[str, Any] = {
            "exit_id": str(gateway_exit.id),
            "exit_server_id": str(exit_protocol.server_id),
            "slot": slot,
            "assigned": 0,
        }
        try:
            # Клиенты, которым выхода не хватало (выдача без выходов), получают его сразу.
            result["assigned"] = await GatewayAssignmentService(self.session, self.actor).assign_unassigned(gateway)
        except (SSHError, CommandExecutionError) as exc:
            result["assign_error"] = True
            logger.warning("assigning waiting clients failed", extra={"error": str(exc)[:200]})
        return result

    async def _create_peer(self, gateway_exit: GatewayExit, exit_server_id: UUID) -> None:
        name = exit_client_name(gateway_exit.id)
        async with ServerLock(exit_server_id):
            ssh_client = await self.connections.create_ssh_client(exit_server_id)
            provider = self.connections.build_provider(ssh_client, ProtocolType.AMNEZIA_WG)
            assert isinstance(provider, AmneziaWGProvider)
            async with ssh_client:
                for client in await provider.list_clients():
                    if client.name == name:
                        # Остаток прерванной попытки: клиент создан, а commit не дошёл. Его конфиг потерян - убираем
                        # и создаём заново, иначе на выходе остался бы лишний peer.
                        await provider.revoke_client(client.public_key)
                created = await provider.create_client(name)
            gateway_exit.client_public_key = created.public_key
            gateway_exit.client_address = created.address
            gateway_exit.encrypted_config = encrypt_secret(created.config)
            gateway_exit.state = ExitState.PEER_CREATED
            await self.session.commit()

    async def _activate(self, gateway_exit: GatewayExit, gateway_server_id: UUID, exit_protocol: VPNProtocol) -> None:
        tunnel = await exit_tunnel(self.session, gateway_exit)
        ssh_client = await self.connections.create_ssh_client(gateway_server_id)
        provider = self.connections.build_provider(ssh_client, ProtocolType.AMNEZIA_GATEWAY)
        assert isinstance(provider, AmneziaGatewayProvider)
        async with ssh_client:
            await provider.apply_exits([tunnel])
        gateway_exit.state = ExitState.ACTIVE
        await self.session.commit()

    async def drain(self, gateway_exit: GatewayExit) -> dict[str, int]:
        """Перенос клиентов снимаемого выхода на остальные до снятия туннеля (`GatewayAssignmentService.drain`).
        Вызывается из `run_remove` между DRAINING и REMOVING; счётчики переноса идут в результат."""
        return await GatewayAssignmentService(self.session, self.actor).drain(gateway_exit)

    async def run_remove(self, job: Job) -> dict[str, Any]:
        """Снятие выхода: DRAINING -> перенос клиентов (`drain`) -> REMOVING -> туннель на шлюзе -> внутренний клиент
        на выходе -> строка удалена. Каждый шаг идемпотентен, после сбоя задачу ставят заново (то же DELETE).

        Недоступный выход (SSH) не мешает: туннель на шлюзе снят, строка удалена, а оставшегося на выходе клиента
        `gw-*` без записи снимет sync выхода. Результат - без ключей.
        """
        if job.server_id is None:
            raise InvalidDataError("Gateway exit job has no server")
        server_id = job.server_id
        gateway = await self._gateway(server_id)
        exit_id = self._exit_id(job)
        gateway_exit = await self.exits.get_for_gateway(gateway.id, exit_id)
        if gateway_exit is None:  # уже снят (повтор после сбоя на последнем шаге)
            return {"exit_id": str(exit_id), "already_removed": True}
        exit_protocol = await self.protocols.get_by_id(gateway_exit.exit_protocol_id)
        slot, public_key = gateway_exit.slot, gateway_exit.client_public_key
        result: dict[str, Any] = {"exit_id": str(exit_id), "slot": slot}

        if gateway_exit.state not in (ExitState.DRAINING, ExitState.REMOVING):
            gateway_exit.state = ExitState.DRAINING
            await self.session.commit()
        if gateway_exit.state == ExitState.DRAINING:
            await self.drain(gateway_exit)
            gateway_exit.state = ExitState.REMOVING
            await self.session.commit()
        # Счётчики переноса хранятся в строке выхода и переживают повторы задачи (перенос мог быть сделан раньше).
        result["moved"], result["unassigned"] = gateway_exit.drained_moved, gateway_exit.drained_unassigned

        ssh_client = await self.connections.create_ssh_client(server_id)
        provider = self.connections.build_provider(ssh_client, ProtocolType.AMNEZIA_GATEWAY)
        assert isinstance(provider, AmneziaGatewayProvider)
        async with ssh_client:
            await provider.remove_exit(slot)

        result["peer_revoked"] = False
        if public_key and exit_protocol is not None:
            result["peer_revoked"] = await self._revoke_peer(exit_protocol.server_id, public_key)
        result["exit_server_id"] = str(exit_protocol.server_id) if exit_protocol else None
        await self.exits.delete(gateway_exit)
        await self.session.commit()
        return result

    async def _revoke_peer(self, exit_server_id: UUID, public_key: str) -> bool:
        """Снимает внутреннего клиента с выхода. False - выход недоступен: peer остаётся, его снимет sync выхода."""
        async with ServerLock(exit_server_id):
            try:
                ssh_client = await self.connections.create_ssh_client(exit_server_id)
                provider = self.connections.build_provider(ssh_client, ProtocolType.AMNEZIA_WG)
                async with ssh_client:
                    await provider.revoke_client(public_key)
            except (SSHError, CredentialNotFound) as exc:
                logger.warning(
                    "exit unreachable, internal client left for sync",
                    extra={"exit_server_id": str(exit_server_id), "error": str(exc)[:200]},
                )
                return False
        return True
