#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from core.exceptions import JobNotFound, JobStatusError, JobTypeError, ServerBusyError
from infrastructure.vpn.provider import ProviderStatus
from infrastructure.workers.locks import ServerLock
from modules.access.models.vpn_credential import VPNCredential
from modules.access.service import VPNCredentialService
from modules.audit.actor import AuditActor
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.jobs.service import JobService
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.servers.health import ServerHealthService
from modules.servers.service import ServerConnectionService
from modules.servers.sync import ServerSyncService


class JobWorker:
    def __init__(self, job_queue: JobQueue):
        self.job_queue = job_queue
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run(self) -> None:
        while not self._stopping:
            await self.process_next()

    async def process_next(self, timeout: int = 2) -> bool:
        """Берёт из очереди одну задачу и выполняет её. False — очередь пуста (ждали timeout секунд).

        Ошибка задачи не роняет воркер: её итог уже записан в Job, а цикл run() идёт дальше.
        """
        job_id = await self.job_queue.dequeue(timeout)
        if job_id is None:
            return False
        try:
            await self._handle(job_id)
        except Exception as exc:
            print(f"Job execution error: {exc}")
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
            # TODO(блок 8): серверная задача (INSTALL_SERVER и т.п.) обязана иметь server_id.
            # Если его нет — это ошибка данных: бросать явное исключение, а не полагаться на то,
            # что в серверных ветках match переменная server_id окажется определена (иначе NameError).
            if job.type in [
                JobType.INSTALL_SERVER,
                JobType.UNINSTALL_SERVER,
                JobType.INSTALL_PROTOCOL,
                JobType.REMOVE_PROTOCOL,
                JobType.CREATE_USER,
                JobType.REVOKE_ACCESS,
                JobType.SYNC_SERVER,
                JobType.HEALTH_CHECK,
            ]:
                if not job.server_id:
                    raise JobTypeError(f"Job with type '{job.type}' must have server_id")
                else:
                    server_id = UUID(str(job.server_id))
            if job.type in [JobType.CREATE_USER, JobType.REVOKE_ACCESS]:
                if not job.access_grant_id:
                    raise JobTypeError(f"Job with type '{job.type}' must have access_grant_id")
                else:
                    access_grant_id = UUID(str(job.access_grant_id))

            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            await session.commit()
            # Аудит результата пишется от имени админа, запустившего задачу (NULL — система).
            actor = AuditActor(admin_id=job.admin_id)

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

                    case JobType.REVOKE_ACCESS:
                        async with ServerLock(server_id):
                            await self.revoke_access(session, access_grant_id, actor)

                    case JobType.INSTALL_SERVER:
                        async with ServerLock(server_id):
                            await self.install_server(session, server_id)

                    case JobType.REMOVE_PROTOCOL:
                        async with ServerLock(server_id):
                            await self.remove_protocol(session, server_id)

                    case JobType.INSTALL_PROTOCOL:
                        async with ServerLock(server_id):
                            await self.install_protocol(session, server_id, ProtocolType.AMNEZIA_WG)

                    case _:
                        raise JobTypeError(f"Unknown type name '{job.type}'")
            except ServerBusyError:
                job.status = JobStatus.PENDING
                await asyncio.sleep(1)
                await self.job_queue.enqueue(job_id)
                await session.commit()

            except Exception as exc:
                job.progress = 0
                job.error = str(exc)
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

    async def remove_protocol(self, session: AsyncSession, server_id: UUID) -> ProviderStatus:
        return await ServerConnectionService(session).protocol_uninstall(server_id)

    async def install_protocol(
        self, session: AsyncSession, server_id: UUID, protocol_type: ProtocolType
    ) -> VPNProtocol:
        protocol = VPNProtocol(server_id=server_id, type=protocol_type)
        return await ServerConnectionService(session).protocol_install(protocol)
