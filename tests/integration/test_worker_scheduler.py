"""Воркер с настоящей Redis-очередью и блокировками; планировщик с настоящими правилами на тестовой БД."""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.vpn.awg_config import list_peers
from infrastructure.workers.locks import ServerLock
from infrastructure.workers.scheduler import (
    Rule,
    Scheduler,
    apply_access_rules,
    fail_stale_jobs,
    schedule_health_checks,
)
from infrastructure.workers.worker import JobWorker
from modules.access.models import AccessGrantStatus as S
from modules.access.schemas import SubscriptionUpdate
from modules.access.service import SubscriptionService
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.jobs.service import JobService
from modules.servers.models.server import HealthStatus, ServerStatus
from tests.fakes.fake_ssh import CONFIG_FILE
from tests.fixtures import World
from tests.integration.conftest import AdminLogin, drain

# --- воркер ----------------------------------------------------------------------------------------------------------


async def test_failed_job_records_error_and_audit(world: World, worker: JobWorker, admin: AdminLogin) -> None:
    server, _, fake = await world.server(install=False)
    fake.reachable = False
    job = await JobService(world.session, AuditActor(admin_id=admin.admin.id)).create(
        JobType.INSTALL_SERVER, server_id=server.id
    )
    assert await drain(worker) == 1
    await world.session.refresh(job)
    await world.session.refresh(server)
    assert job.status == JobStatus.FAILED and "Connection error" in (job.error or "")
    assert job.started_at and job.finished_at and job.progress == 0
    assert server.status == ServerStatus.ERROR
    (failed,) = await world.audit(AuditAction.SERVER_INSTALL_FAILED)
    assert failed.admin_id == admin.admin.id  # от имени запустившего, хотя пишет воркер


async def test_busy_server_requeues_job(world: World, worker: JobWorker) -> None:
    server, _, _ = await world.server()
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.SYNC_SERVER, server_id=server.id)
    async with ServerLock(server.id, blocking_timeout=0):
        assert await worker.process_next(timeout=1)  # не дождался блокировки → вернул задачу в очередь
        await world.session.refresh(job)
        assert job.status == JobStatus.PENDING
        assert await JobQueue().size() == 1
    assert await drain(worker) == 1  # блокировку отпустили — задача выполнилась
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS and job.result is not None


async def test_server_job_without_server_id_is_rejected(world: World, worker: JobWorker) -> None:
    job = Job(type=JobType.INSTALL_SERVER, server_id=None)
    world.session.add(job)
    await world.session.commit()
    await JobQueue().enqueue(job.id)
    assert await worker.process_next(timeout=1)  # ошибка данных не роняет воркер
    await world.session.refresh(job)
    assert job.status == JobStatus.PENDING and job.started_at is None


async def test_already_finished_job_is_not_run_twice(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server()
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.SYNC_SERVER, server_id=server.id)
    await drain(worker)
    commands = len(fake.commands)
    await JobQueue().enqueue(job.id)  # тот же id попал в очередь повторно
    await drain(worker)
    assert len(fake.commands) == commands  # повтор не пошёл на сервер


# --- планировщик -------------------------------------------------------------------------------------------------


async def test_rules_run_once_per_interval_across_schedulers() -> None:
    calls: list[str] = []

    async def counted() -> str:
        calls.append("tick")
        return "ok"

    async def broken() -> str:
        raise RuntimeError("boom")

    rules = [Rule("broken", 60, broken), Rule("counted", 60, counted), Rule("disabled", 0, counted)]
    first, second = Scheduler(rules, 1), Scheduler(rules, 1)
    await asyncio.gather(first.run_due(), second.run_due())
    await first.run_due()
    assert calls == ["tick"]  # один раз за интервал на всех; сбой «broken» не помешал; «disabled» не запускался


async def test_scheduler_stops_promptly() -> None:
    scheduler = Scheduler([], tick_seconds=3600)
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.1)
    scheduler.stop()
    await asyncio.wait_for(task, timeout=2)  # не досыпает час до следующего тика


async def test_access_rules_expire_and_revoke_on_server(world: World, worker: JobWorker) -> None:
    _, protocol, fake = await world.server()
    _, subscription, grant, credential = await world.issued(protocol)
    _, starting, _ = await world.access(protocol, starts=timedelta(seconds=-1))  # PENDING, период уже начался
    await SubscriptionService(world.session, SYSTEM_ACTOR).update(
        subscription.id, SubscriptionUpdate(valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    )

    summary = await apply_access_rules()
    assert summary == "expired 1/1, activated 1/1"
    await drain(worker)  # задача REVOKE_ACCESS, поставленная истечением

    for row in (subscription, grant, credential, starting):
        await world.session.refresh(row)
    assert (subscription.status, grant.status, starting.status) == (S.EXPIRED, S.EXPIRED, S.ACTIVE)
    assert credential.revoked_at is not None
    assert credential.client_public_key not in {peer.public_key for peer in list_peers(fake.files[CONFIG_FILE])}
    assert [log.admin_id for log in await world.audit(AuditAction.CREDENTIAL_REVOKED)] == [None]  # система


async def test_periodic_health_rule_updates_servers(world: World, worker: JobWorker) -> None:
    server, _, _ = await world.server()
    assert await schedule_health_checks() == "queued 1 health checks"
    await drain(worker)
    await world.session.refresh(server)
    assert server.health_status == HealthStatus.OK


async def test_stale_jobs_rule(world: World, session: AsyncSession) -> None:
    server, _, _ = await world.server()
    stale = Job(
        type=JobType.SYNC_SERVER,
        server_id=server.id,
        status=JobStatus.RUNNING,
        started_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    session.add(stale)
    await session.commit()
    assert await fail_stale_jobs(1800) == "marked 1 stale running jobs as failed"
    await session.refresh(stale)
    assert stale.status == JobStatus.FAILED
