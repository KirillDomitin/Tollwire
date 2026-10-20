#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from core.log import redact
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.jobs.repository import JobRepository
from modules.protocols.models.protocol import ProtocolType
from modules.servers.models.server import ServerStatus, VPNServer

# Какие задачи пишут в аудит старт и завершение. Выдача/отзыв клиента пишут успех сами (там известен
# credential), здесь — только их провал: недошедший отзыв означает, что доступ на сервере ещё жив.
_STARTED: dict[JobType, AuditAction] = {
    JobType.INSTALL_SERVER: AuditAction.SERVER_INSTALL_STARTED,
    JobType.INSTALL_PROTOCOL: AuditAction.PROTOCOL_INSTALL_STARTED,
    JobType.REMOVE_PROTOCOL: AuditAction.PROTOCOL_REMOVE_STARTED,
    JobType.SYNC_SERVER: AuditAction.SERVER_SYNC_STARTED,
    JobType.INSTALL_SSH_KEY: AuditAction.SERVER_SSH_KEY_INSTALL_STARTED,
    JobType.DISCOVER_VPN: AuditAction.SERVER_DISCOVER_STARTED,
    JobType.IMPORT_CLIENTS: AuditAction.CLIENTS_IMPORT_STARTED,
    JobType.GATEWAY_ADD_EXIT: AuditAction.GATEWAY_EXIT_ADD_STARTED,
    JobType.GATEWAY_REMOVE_EXIT: AuditAction.GATEWAY_EXIT_REMOVE_STARTED,
}
_COMPLETED: dict[JobType, AuditAction] = {
    JobType.INSTALL_SERVER: AuditAction.SERVER_INSTALL_COMPLETED,
    JobType.INSTALL_PROTOCOL: AuditAction.PROTOCOL_INSTALL_COMPLETED,
    JobType.REMOVE_PROTOCOL: AuditAction.PROTOCOL_REMOVE_COMPLETED,
    JobType.SYNC_SERVER: AuditAction.SERVER_SYNC_COMPLETED,
    JobType.INSTALL_SSH_KEY: AuditAction.SERVER_SSH_KEY_INSTALL_COMPLETED,
    JobType.DISCOVER_VPN: AuditAction.SERVER_DISCOVER_COMPLETED,
    JobType.IMPORT_CLIENTS: AuditAction.CLIENTS_IMPORT_COMPLETED,
    JobType.GATEWAY_ADD_EXIT: AuditAction.GATEWAY_EXIT_ADD_COMPLETED,
    JobType.GATEWAY_REMOVE_EXIT: AuditAction.GATEWAY_EXIT_REMOVE_COMPLETED,
}
_FAILED: dict[JobType, AuditAction] = {
    JobType.INSTALL_SERVER: AuditAction.SERVER_INSTALL_FAILED,
    JobType.INSTALL_PROTOCOL: AuditAction.PROTOCOL_INSTALL_FAILED,
    JobType.REMOVE_PROTOCOL: AuditAction.PROTOCOL_REMOVE_FAILED,
    JobType.SYNC_SERVER: AuditAction.SERVER_SYNC_FAILED,
    JobType.CREATE_USER: AuditAction.CREDENTIAL_ISSUE_FAILED,
    JobType.REVOKE_ACCESS: AuditAction.CREDENTIAL_REVOKE_FAILED,
    JobType.INSTALL_SSH_KEY: AuditAction.SERVER_SSH_KEY_INSTALL_FAILED,
    JobType.DISCOVER_VPN: AuditAction.SERVER_DISCOVER_FAILED,
    JobType.IMPORT_CLIENTS: AuditAction.CLIENTS_IMPORT_FAILED,
    JobType.GATEWAY_ADD_EXIT: AuditAction.GATEWAY_EXIT_ADD_FAILED,
    JobType.GATEWAY_REMOVE_EXIT: AuditAction.GATEWAY_EXIT_REMOVE_FAILED,
}
# Задачи, чей result (без секретов) целиком попадает в meta аудита: итог виден в журнале, а не только в задаче.
_RESULT_IN_AUDIT = frozenset({JobType.INSTALL_SSH_KEY})
_IMPORT_COUNTED = ("imported", "already", "gone", "unsupported", "conflict")
# Задачи выходов шлюза: в meta аудита идут только эти поля params (id выхода и сервера-выхода, слот, вес) - не ключи.
_EXIT_JOBS = (JobType.GATEWAY_ADD_EXIT, JobType.GATEWAY_REMOVE_EXIT)
_EXIT_META_FIELDS = ("exit_id", "exit_server_id", "slot", "weight")
_ERROR_MAX_LENGTH = 500


def job_error_text(exc: BaseException) -> str:
    """Текст ошибки для Job.error: его видит в API даже viewer. В сообщении бывает stderr команды с сервера —
    маскируем секреты (как в логах) и обрезаем."""
    return redact(str(exc) or type(exc).__name__)[:1000]


logger = logging.getLogger("jobs")


class JobService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.jobs = JobRepository(session)
        self.job_queue = JobQueue()
        self.audit = AuditService(session, actor)
        self.actor = actor

    async def create(
        self,
        job_type: JobType,
        server_id: UUID | None,
        access_grant_id: UUID | None = None,
        params: dict[str, Any] | None = None,
    ) -> Job:
        """params - входные данные задачи. Без секретов: хранятся в БД открытым текстом, в аудит не попадают,
        JobRead их не отдаёт."""
        job = Job(
            type=job_type,
            server_id=server_id,
            access_grant_id=access_grant_id,
            admin_id=self.actor.admin_id,
            params=params,
        )
        await self.jobs.create(job)
        await self.session.flush()
        if job_type in _STARTED:
            started_meta: dict[str, Any] = {"job_id": job.id, **self._job_meta(job_type, params)}
            self.audit.record(_STARTED[job_type], AuditEntity.SERVER, server_id, started_meta)
        await self.session.commit()
        await self.job_queue.enqueue(job.id)
        # В API здесь же в контексте request_id: по этой строке запрос связывается с job_id в логах воркера.
        logger.info(
            "job queued", extra={"job_id": str(job.id), "job_type": job_type.value, "server_id": str(server_id)}
        )
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
            if job.type == JobType.INSTALL_SERVER and job.server_id:
                # Воркер убили посреди установки: сервер остался бы INSTALLING навсегда (и выпал бы из health
                # и sync). ERROR — понятно, что установку надо повторить.
                server = await self.session.get(VPNServer, job.server_id)
                if server is not None and server.status == ServerStatus.INSTALLING:
                    server.status = ServerStatus.ERROR
        await self.session.commit()
        return len(stale)

    async def requeue_lost_pending(self, max_age_seconds: int) -> int:
        """PENDING дольше max_age и нет в очереди Redis — задача потеряна (Redis перезапустился без данных или
        enqueue не дошёл после commit). Ставим её в очередь снова (вызывает планировщик).

        Редкий дубль в очереди безопасен: воркер захватывает задачу под блокировкой строки (см. JobWorker).
        """
        before = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        requeued = 0
        for job in await self.jobs.list_pending_updated_before(before):
            if not await self.job_queue.contains(job.id):
                await self.job_queue.enqueue(job.id)
                requeued += 1
        return requeued

    async def purge_finished(self, older_than_days: int) -> int:
        """Удаляет завершённые задачи старше N дней (вызывает планировщик). 0 — хранить вечно."""
        if older_than_days <= 0:
            return 0
        deleted = await self.jobs.delete_finished_before(datetime.now(timezone.utc) - timedelta(days=older_than_days))
        await self.session.commit()
        return deleted

    @staticmethod
    def _job_meta(job_type: JobType, params: dict[str, Any] | None) -> dict[str, Any]:
        """Несекретные поля params для аудита. Протокольные задачи: какой протокол ставили/снимали (значение enum, без
        params - обычный; другого в журнал не идёт). Задачи выходов шлюза: id выхода и сервера-выхода, слот и вес."""
        if job_type in _EXIT_JOBS:
            return {key: params[key] for key in _EXIT_META_FIELDS if params and key in params}
        if job_type not in (JobType.INSTALL_PROTOCOL, JobType.REMOVE_PROTOCOL):
            return {}
        try:
            return {
                "protocol_type": ProtocolType((params or {}).get("protocol_type", ProtocolType.AMNEZIA_WG.value)).value
            }
        except ValueError:
            return {}

    @staticmethod
    def _meta(job: Job) -> dict[str, Any]:
        meta: dict[str, Any] = {"job_id": job.id, **JobService._job_meta(job.type, job.params)}
        if job.type in _RESULT_IN_AUDIT and job.result:
            meta.update(job.result)
        if job.type == JobType.DISCOVER_VPN and job.result:
            found = job.result.get("protocols", [])
            meta["protocols"] = len(found)
            meta["clients"] = sum(len(item["clients"]) for item in found)
        if job.type == JobType.IMPORT_CLIENTS and job.result:
            # Только id протокола и счётчики: имена и ключи клиентов в журнал не попадают.
            meta["protocol_id"] = job.result.get("protocol_id")
            for name in _IMPORT_COUNTED:
                meta[name] = len(job.result.get(name, []))
        return meta

    async def get(self, job_id: UUID) -> Job:
        job = await self.jobs.get_by_id(job_id)
        if not job:
            raise NotFoundError
        return job

    async def list_all(
        self, server_id: UUID | None, status: JobStatus | None, limit: int = 50, offset: int = 0
    ) -> Sequence[Job]:
        return await self.jobs.list_jobs(server_id, status, limit, offset)
