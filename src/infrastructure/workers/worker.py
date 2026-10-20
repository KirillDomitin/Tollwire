#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, NoReturn
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from core.exceptions import AppError, JobError, JobNotFound, JobStatusError, JobTypeError, ServerBusyError
from core.log import bind
from core.ssh_exception import SSHServerError
from infrastructure.vpn.provider import ProviderStatus
from infrastructure.workers.locks import ServerLock
from modules.access.import_service import ClientImportService
from modules.access.models.vpn_credential import VPNCredential
from modules.access.service import VPNCredentialService
from modules.audit.actor import AuditActor
from modules.gateway.ru import GatewayRuService
from modules.gateway.service import GatewayExitService
from modules.geoip.service import GeoipService
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.jobs.service import JobService, job_error_text
from modules.protocols.models.protocol import INSTALLABLE_PROTOCOLS, ProtocolType, VPNProtocol
from modules.servers.discovery import ServerDiscoveryService
from modules.servers.health import ServerHealthService
from modules.servers.service import ServerConnectionService
from modules.servers.ssh_key import ServerSshKeyService
from modules.servers.sync import ServerSyncService

logger = logging.getLogger("worker")


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class JobWorker:
    def __init__(self, job_queue: JobQueue):
        self.job_queue = job_queue
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                await self.process_next()
                backoff = 1.0
            except (RedisError, OSError) as exc:
                # Redis недоступен (перезапуск, сеть). Раньше исключение роняло весь процесс воркера вместе с
                # планировщиком. Ждём и пробуем снова; потерянное в очереди подберёт правило requeue_jobs.
                logger.warning("queue unavailable, retrying", extra={"error": str(exc), "retry_in": backoff})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def process_next(self, timeout: int = 2) -> bool:
        """Берёт из очереди одну задачу и выполняет её. False — очередь пуста (ждали timeout секунд).

        Ошибка задачи не роняет воркер: её итог уже записан в Job, а цикл run() идёт дальше.
        """
        job_id = await self.job_queue.dequeue(timeout)
        if job_id is None:
            return False
        with bind(job_id=job_id):
            try:
                await self._handle(job_id)
            except JobError as exc:
                # Ошибка данных задачи (не найдена, уже не PENDING, нет server_id) — задача не выполнялась.
                logger.warning("job skipped", extra={"reason": str(exc)})
            except Exception:
                logger.exception("job handling crashed")
        return True

    async def _handle(self, job_id: UUID) -> None:
        async with session_factory() as session:
            # FOR UPDATE: если один id попал в очередь дважды и его взяли два воркера, второй ждёт commit
            # первого, видит RUNNING и отказывается. Без блокировки оба прочли бы PENDING и выполнили задачу.
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise JobNotFound(f"Job {job_id} not found")
            if job.status != JobStatus.PENDING:
                raise JobStatusError("Job status not 'PENDING'")
            # Серверная задача обязана иметь server_id (а задача по доступу — access_grant_id). Нет — ошибка
            # данных (например, сервер удалили, пока задача ждала: FK обнулил server_id). Такую задачу сразу
            # закрываем FAILED: оставленная в PENDING, она бы вечно возвращалась в очередь правилом requeue_jobs.
            if job.type in [
                JobType.INSTALL_SERVER,
                JobType.UNINSTALL_SERVER,
                JobType.INSTALL_PROTOCOL,
                JobType.REMOVE_PROTOCOL,
                JobType.CREATE_USER,
                JobType.REVOKE_ACCESS,
                JobType.SYNC_SERVER,
                JobType.HEALTH_CHECK,
                JobType.INSTALL_SSH_KEY,
                JobType.DISCOVER_VPN,
                JobType.IMPORT_CLIENTS,
                JobType.GATEWAY_ADD_EXIT,
                JobType.GATEWAY_REMOVE_EXIT,
                JobType.GATEWAY_APPLY_RU,
            ]:
                if not job.server_id:
                    await self._reject(session, job, f"Job with type '{job.type.value}' must have server_id")
                else:
                    server_id = UUID(str(job.server_id))
            if job.type in [JobType.CREATE_USER, JobType.REVOKE_ACCESS]:
                if not job.access_grant_id:
                    await self._reject(session, job, f"Job with type '{job.type.value}' must have access_grant_id")
                else:
                    access_grant_id = UUID(str(job.access_grant_id))

            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            await session.commit()
            # Аудит результата пишется от имени админа, запустившего задачу (NULL — система).
            actor = AuditActor(admin_id=job.admin_id)
            started = time.perf_counter()
            with bind(job_type=job.type.value, server_id=job.server_id):
                logger.info("job started")

                try:
                    match job.type:
                        case JobType.CREATE_USER:
                            async with ServerLock(server_id):
                                await self.create_user(session, access_grant_id, actor)

                        case JobType.DELETE_USER:
                            await self.delete_user()

                        case JobType.SYNC_SERVER:
                            async with ServerLock(server_id):
                                job.result = await self.sync_server(session, server_id)

                        case JobType.HEALTH_CHECK:
                            # Без ServerLock: health только читает и не должен ждать выдачу/отзыв клиентов.
                            job.result = await self.health_check(session, server_id)

                        case JobType.DISCOVER_VPN:
                            job.result = await ServerDiscoveryService(session).discover(server_id)

                        case JobType.IMPORT_CLIENTS:
                            async with ServerLock(server_id):
                                job.result = await ClientImportService(session, actor).run(job)

                        case JobType.GATEWAY_ADD_EXIT:
                            # ServerLock шлюза; выход блокируется внутри сервиса (порядок всегда шлюз -> выход).
                            async with ServerLock(server_id):
                                job.result = await GatewayExitService(session, actor).run_add(job)

                        case JobType.GATEWAY_REMOVE_EXIT:
                            async with ServerLock(server_id):
                                job.result = await GatewayExitService(session, actor).run_remove(job)

                        case JobType.GATEWAY_APPLY_RU:
                            async with ServerLock(server_id):
                                job.result = await GatewayRuService(session, actor).run_apply(job)

                        case JobType.GEOIP_UPDATE:
                            # Глобальная задача без сервера: качает список; применение на шлюзах ставит сам сервис.
                            job.result = await GeoipService(session, actor).run_update()

                        case JobType.REVOKE_ACCESS:
                            async with ServerLock(server_id):
                                await self.revoke_access(session, access_grant_id, actor)

                        case JobType.INSTALL_SERVER:
                            async with ServerLock(server_id):
                                await self.install_server(session, server_id)
                                try:
                                    job.result = await ServerDiscoveryService(session).discover(server_id)
                                except Exception as discovery_error:
                                    await session.rollback()  # установка уже закоммичена; сбой поиска её не отменяет
                                    await session.refresh(job)  # rollback сбросил состояние объекта: перечитываем
                                    logger.warning(
                                        "discovery after install failed", extra={"error": str(discovery_error)[:300]}
                                    )

                        case JobType.REMOVE_PROTOCOL:
                            protocol_type = self._protocol_type(job)
                            async with ServerLock(server_id):
                                await self.remove_protocol(session, server_id, protocol_type)

                        case JobType.INSTALL_PROTOCOL:
                            protocol_type = self._protocol_type(job)
                            async with ServerLock(server_id):
                                await self.install_protocol(session, server_id, protocol_type)

                        case JobType.INSTALL_SSH_KEY:
                            async with ServerLock(server_id):
                                job.result = await ServerSshKeyService(session, actor).install(server_id)

                        case _:
                            raise JobTypeError(f"Unknown type name '{job.type}'")
                except ServerBusyError:
                    logger.info("server busy, job requeued")
                    job.status = JobStatus.PENDING
                    await asyncio.sleep(1)
                    await self.job_queue.enqueue(job_id)
                    await session.commit()

                except Exception as exc:
                    # Ожидаемые сбои (SSH недоступен, ошибка на сервере) — без трейсбека; неожиданные — с ним.
                    expected = isinstance(exc, (AppError, SSHServerError))
                    logger.warning(
                        "job failed",
                        extra={"error": str(exc)[:500], "duration_ms": _elapsed_ms(started)},
                        exc_info=not expected,
                    )
                    # Сначала rollback: после ошибки БД сессия непригодна (commit бросил бы PendingRollbackError
                    # и задача висела бы в RUNNING), а после любой ошибки незавершённые изменения сервиса не
                    # должны уйти в базу вместе с FAILED. Сервисы, которым нужно сохранить итог сбоя (статус
                    # сервера ERROR/OFFLINE), коммитят его сами до исключения.
                    await session.rollback()
                    reloaded = await session.get(Job, job_id)
                    assert reloaded is not None
                    job = reloaded
                    job.progress = 0
                    job.error = job_error_text(exc)
                    # Несекретные подробности сбоя от сервиса (например, откатилась ли установка ключа).
                    job.result = exc.job_result if isinstance(exc, AppError) else None
                    job.status = JobStatus.FAILED
                    job.finished_at = datetime.now(timezone.utc)
                    JobService(session, actor).record_finished(job)
                    await session.commit()
                else:
                    job.status = JobStatus.SUCCESS
                    job.finished_at = datetime.now(timezone.utc)
                    job.progress = 100
                    JobService(session, actor).record_finished(job)
                    await session.commit()
                    logger.info("job succeeded", extra={"duration_ms": _elapsed_ms(started)})

    @staticmethod
    async def _reject(session: AsyncSession, job: Job, reason: str) -> NoReturn:
        job.status = JobStatus.FAILED
        job.error = reason
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        raise JobTypeError(reason)

    async def create_user(self, session: AsyncSession, access_grant_id: UUID, actor: AuditActor) -> VPNCredential:
        return await VPNCredentialService(session, actor).provision(access_grant_id)

    async def delete_user(self) -> None:
        pass

    async def sync_server(self, session: AsyncSession, server_id: UUID) -> dict[str, Any]:
        return await ServerSyncService(session).sync(server_id)

    async def health_check(self, session: AsyncSession, server_id: UUID) -> dict[str, Any]:
        return await ServerHealthService(session).check(server_id)

    async def revoke_access(
        self, session: AsyncSession, access_grant_id: UUID, actor: AuditActor
    ) -> VPNCredential | None:
        return await VPNCredentialService(session, actor).deprovision(access_grant_id)

    async def install_server(self, session: AsyncSession, server_id: UUID) -> None:
        return await ServerConnectionService(session).server_install(server_id)

    async def remove_protocol(
        self, session: AsyncSession, server_id: UUID, protocol_type: ProtocolType = ProtocolType.AMNEZIA_WG
    ) -> ProviderStatus:
        return await ServerConnectionService(session).protocol_uninstall(server_id, protocol_type)

    @staticmethod
    def _protocol_type(job: Job) -> ProtocolType:
        """Какой протокол ставить/снимать: `Job.params["protocol_type"]`, без params - обычный (AMNEZIA_WG)."""
        value = (job.params or {}).get("protocol_type", ProtocolType.AMNEZIA_WG.value)
        try:
            protocol_type = ProtocolType(value)
        except ValueError:
            raise JobTypeError(f"Unknown protocol type {str(value)[:50]!r}") from None
        if protocol_type not in INSTALLABLE_PROTOCOLS:
            raise JobTypeError(f"Protocol '{protocol_type.value}' cannot be installed or removed by the panel")
        return protocol_type

    async def install_protocol(
        self, session: AsyncSession, server_id: UUID, protocol_type: ProtocolType
    ) -> VPNProtocol:
        protocol = VPNProtocol(server_id=server_id, type=protocol_type)
        installed = await ServerConnectionService(session).protocol_install(protocol)
        if protocol_type == ProtocolType.AMNEZIA_GATEWAY:
            # Переустановленный шлюз: российские маршруты вернуть из активного списка (сбой не отменяет установку).
            await GatewayRuService(session, AuditActor(admin_id=None)).ensure_server(server_id)
        return installed
