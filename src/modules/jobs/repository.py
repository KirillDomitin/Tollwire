#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.jobs.models.job import Job, JobStatus, JobType


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, job: Job) -> None:  # Добавил для сохранения стиля
        self.session.add(job)

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

    async def list_running_started_before(self, before: datetime) -> Sequence[Job]:
        query = select(Job).where(Job.status == JobStatus.RUNNING, Job.started_at < before)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def list_pending_updated_before(self, before: datetime) -> Sequence[Job]:
        # updated_at, а не created_at: задача, которую воркер вернул в очередь из-за занятого сервера,
        # обновляется при каждом возврате и сюда не попадает.
        query = select(Job).where(Job.status == JobStatus.PENDING, Job.updated_at < before)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def delete_finished_before(self, before: datetime) -> int:
        """Удаляет завершённые задачи, созданные раньше before. PENDING/RUNNING не трогает."""
        finished = (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED)
        query = delete(Job).where(Job.status.in_(finished), Job.created_at < before)
        result = cast(CursorResult[Any], await self.session.execute(query))
        return int(result.rowcount)

    async def get_last_for_grant(self, job_type: JobType, access_grant_id: UUID) -> Job | None:
        query = (
            select(Job)
            .where(Job.type == job_type, Job.access_grant_id == access_grant_id)
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def has_unfinished_of_type(self, job_type: JobType) -> bool:
        """Незавершённая задача этого типа есть (для глобальных задач без сервера)."""
        query = (
            select(Job.id).where(Job.type == job_type, Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING))).limit(1)
        )
        return (await self.session.execute(query)).first() is not None

    async def has_unfinished_for_server(self, job_type: JobType, server_id: UUID) -> bool:
        query = (
            select(Job.id)
            .where(
                Job.type == job_type,
                Job.server_id == server_id,
                Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
            )
            .limit(1)
        )
        return (await self.session.execute(query)).first() is not None

    async def has_unfinished_for_grant(self, job_type: JobType, access_grant_id: UUID) -> bool:
        query = (
            select(Job.id)
            .where(
                Job.type == job_type,
                Job.access_grant_id == access_grant_id,
                Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
            )
            .limit(1)
        )
        return (await self.session.execute(query)).first() is not None
