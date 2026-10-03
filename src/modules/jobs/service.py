#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.jobs.repository import JobRepository

# Какие задачи пишут в аудит старт и завершение. Выдача/отзыв клиента пишут успех сами (там известен
# credential), здесь — только их провал: недошедший отзыв означает, что доступ на сервере ещё жив.
_STARTED: dict[JobType, AuditAction] = {
    JobType.INSTALL_SERVER: AuditAction.SERVER_INSTALL_STARTED,
    JobType.INSTALL_PROTOCOL: AuditAction.PROTOCOL_INSTALL_STARTED,
    JobType.REMOVE_PROTOCOL: AuditAction.PROTOCOL_REMOVE_STARTED,
    JobType.SYNC_SERVER: AuditAction.SERVER_SYNC_STARTED,
}
_COMPLETED: dict[JobType, AuditAction] = {
    JobType.INSTALL_SERVER: AuditAction.SERVER_INSTALL_COMPLETED,
    JobType.INSTALL_PROTOCOL: AuditAction.PROTOCOL_INSTALL_COMPLETED,
    JobType.REMOVE_PROTOCOL: AuditAction.PROTOCOL_REMOVE_COMPLETED,
    JobType.SYNC_SERVER: AuditAction.SERVER_SYNC_COMPLETED,
}
_FAILED: dict[JobType, AuditAction] = {
    JobType.INSTALL_SERVER: AuditAction.SERVER_INSTALL_FAILED,
    JobType.INSTALL_PROTOCOL: AuditAction.PROTOCOL_INSTALL_FAILED,
    JobType.REMOVE_PROTOCOL: AuditAction.PROTOCOL_REMOVE_FAILED,
    JobType.SYNC_SERVER: AuditAction.SERVER_SYNC_FAILED,
    JobType.CREATE_USER: AuditAction.CREDENTIAL_ISSUE_FAILED,
    JobType.REVOKE_ACCESS: AuditAction.CREDENTIAL_REVOKE_FAILED,
}
_ERROR_MAX_LENGTH = 500


class JobService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.jobs = JobRepository(session)
        self.job_queue = JobQueue()
        self.audit = AuditService(session, actor)
        self.actor = actor

    async def create(self, job_type: JobType, server_id: UUID | None, access_grant_id: UUID | None = None) -> Job:
        job = Job(type=job_type, server_id=server_id, access_grant_id=access_grant_id, admin_id=self.actor.admin_id)
        await self.jobs.create(job)
        await self.session.flush()
        if job_type in _STARTED:
            self.audit.record(_STARTED[job_type], AuditEntity.SERVER, server_id, {"job_id": job.id})
        await self.session.commit()
        await self.job_queue.enqueue(job.id)
        return job

    def record_finished(self, job: Job) -> None:
        """Аудит завершения задачи (вызывает воркер перед финальным commit). Актор — инициатор задачи."""
        if job.status == JobStatus.SUCCESS and job.type in _COMPLETED:
            self.audit.record(_COMPLETED[job.type], AuditEntity.SERVER, job.server_id, self._meta(job))
        elif job.status == JobStatus.FAILED and job.type in _FAILED:
            meta = self._meta(job)
            meta["error"] = (job.error or "")[:_ERROR_MAX_LENGTH]
            if job.access_grant_id:
                meta["access_grant_id"] = job.access_grant_id
            self.audit.record(_FAILED[job.type], AuditEntity.SERVER, job.server_id, meta)

    async def fail_stale_running(self, max_age_seconds: int) -> int:
        """RUNNING дольше max_age — воркер умер посреди задачи. Переводим в FAILED (вызывает планировщик).

        Иначе такая задача навсегда считается «незавершённой» и блокирует плановые проверки сервера.
        """
        now = datetime.now(timezone.utc)
        stale = await self.jobs.list_running_started_before(now - timedelta(seconds=max_age_seconds))
        for job in stale:
            job.status = JobStatus.FAILED
            job.error = "Job was interrupted: worker stopped while it was running"
            job.finished_at = now
            self.record_finished(job)
        await self.session.commit()
        return len(stale)

    @staticmethod
    def _meta(job: Job) -> dict[str, Any]:
        return {"job_id": job.id}

    async def get(self, job_id: UUID) -> Job:
        job = await self.jobs.get_by_id(job_id)
        if not job:
            raise NotFoundError
        return job

    async def list_all(
        self, server_id: UUID | None, status: JobStatus | None, limit: int = 50, offset: int = 0
    ) -> Sequence[Job]:
        return await self.jobs.list_jobs(server_id, status, limit, offset)
