#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from modules.jobs.models.job import Job, JobStatus
from modules.jobs.repository import JobRepository


class JobService:
    def __init__(self, session: AsyncSession):
        self.jobs = JobRepository(session)

    async def get(self, job_id: UUID) -> Job:
        job = await self.jobs.get_by_id(job_id)
        if not job:
            raise NotFoundError
        return job

    async def list_all(
        self, server_id: UUID | None, status: JobStatus | None, limit: int = 50, offset: int = 0
    ) -> Sequence[Job]:
        return await self.jobs.list_jobs(server_id, status, limit, offset)
