#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.jobs.models.job import Job, JobStatus


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, job_id: UUID) -> Job | None:
        return await self.session.get(Job, job_id)

    async def list_jobs(
        self, server_id: UUID | None, status: JobStatus | None, limit: int = 50, offset: int = 0
    ) -> Sequence[Job]:
        query = select(Job)
        if server_id:
            query = query.where(Job.server_id == server_id)
        if status:
            query = query.where(Job.status == status)

        query = query.order_by(Job.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return result.scalars().all()
