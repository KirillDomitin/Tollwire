#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timezone
from uuid import UUID

from core.database.postgres import session_factory
from core.exceptions import JobNotFound, JobStatusError, JobTypeError, ServerBusyError
from infrastructure.workers.locks import ServerLock
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue


class JobWorker:
    def __init__(self, job_queue: JobQueue):
        self.job_queue = job_queue
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run(self) -> None:
        while not self._stopping:
            job_id = await self.job_queue.dequeue()
            if job_id is None:
                continue
            try:
                await self._handle(job_id)
            except Exception as exc:
                print(f"Job execution error: {exc}")
                continue

    async def _handle(self, job_id: UUID) -> None:
        async with session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                raise JobNotFound(f"Job {job_id} not found")
            if job.status != JobStatus.PENDING:
                raise JobStatusError("Job status not 'PENDING'")
            # TODO(блок 8): серверная задача (INSTALL_SERVER и т.п.) обязана иметь server_id.
            # Если его нет — это ошибка данных: бросать явное исключение, а не полагаться на то,
            # что в серверных ветках match переменная server_id окажется определена (иначе NameError).
            if job.server_id:
                server_id = UUID(str(job.server_id))

            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            await session.commit()

            try:
                match job.type:
                    case JobType.CREATE_USER:
                        await self.create_user()
                    case JobType.DELETE_USER:
                        await self.delete_user()
                    case JobType.SYNC_SERVER:
                        await self.sync_server()
                    case JobType.HEALTH_CHECK:
                        await self.health_check()
                    case JobType.REVOKE_ACCESS:
                        await self.revoke_access()
                    case JobType.INSTALL_SERVER:
                        async with ServerLock(server_id):
                            await self.install_server()
                    case JobType.REMOVE_PROTOCOL:
                        async with ServerLock(server_id):
                            await self.remove_protocol()
                    case JobType.UNINSTALL_SERVER:
                        async with ServerLock(server_id):
                            await self.uninstall_server()
                    case JobType.INSTALL_PROTOCOL:
                        async with ServerLock(server_id):
                            await self.install_protocol()
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
                await session.commit()
            else:
                job.status = JobStatus.SUCCESS
                job.finished_at = datetime.now(timezone.utc)
                job.progress = 100
                await session.commit()

    async def create_user(self) -> None:
        pass

    async def delete_user(self) -> None:
        pass

    async def sync_server(self) -> None:
        pass

    async def health_check(self) -> None:
        pass

    async def revoke_access(self) -> None:
        pass

    async def install_server(self) -> None:
        pass

    async def remove_protocol(self) -> None:
        pass

    async def uninstall_server(self) -> None:
        pass

    async def install_protocol(self) -> None:
        pass
