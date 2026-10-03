#!/usr/bin/python
# -*- coding: utf-8 -*-
from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit.actor import SYSTEM_ACTOR
from modules.jobs.models.job import Job, JobType
from modules.jobs.repository import JobRepository
from modules.jobs.service import JobService
from modules.servers.repository import ServerRepository


class ServerMaintenanceService:
    """Плановые проверки серверов (вызывает планировщик): ставит задачи HEALTH_CHECK / SYNC_SERVER.

    Задачи от имени системы (admin_id = NULL). Если по серверу уже ждёт или идёт такая же задача,
    новую не ставим: медленный или недоступный сервер не должен копить очередь.
    """

    def __init__(self, session: AsyncSession):
        self.servers = ServerRepository(session)
        self.job_repository = JobRepository(session)
        self.jobs = JobService(session, SYSTEM_ACTOR)

    async def schedule_health_checks(self) -> list[Job]:
        return await self._schedule(JobType.HEALTH_CHECK, with_protocol=False)

    async def schedule_syncs(self) -> list[Job]:
        # Sync меняет сервер (восстанавливает/удаляет peer'ы) — только там, где есть наш протокол.
        return await self._schedule(JobType.SYNC_SERVER, with_protocol=True)

    async def _schedule(self, job_type: JobType, with_protocol: bool) -> list[Job]:
        jobs = []
        for server_id in await self.servers.list_checkable_ids(with_protocol=with_protocol):
            if await self.job_repository.has_unfinished_for_server(job_type, server_id):
                continue
            jobs.append(await self.jobs.create(job_type, server_id=server_id))
        return jobs
