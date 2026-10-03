"""Воркер с настоящей Redis-очередью и блокировками; планировщик с настоящими правилами на тестовой БД."""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from core.redis import get_redis
from infrastructure.vpn.awg_config import list_peers
from infrastructure.workers.locks import ServerLock
from infrastructure.workers.scheduler import (
    Rule,
    Scheduler,
    apply_access_rules,
    apply_retention,
    fail_stale_jobs,
    requeue_lost_jobs,
    schedule_health_checks,
)
from infrastructure.workers.worker import JobWorker
from modules.access.models import AccessGrantStatus as S
from modules.access.schemas import SubscriptionUpdate
from modules.access.service import SubscriptionService
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import KEY as QUEUE_KEY
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
    # Закрыта FAILED, а не оставлена в PENDING: иначе requeue_jobs возвращал бы её в очередь вечно.
    assert job.status == JobStatus.FAILED and job.started_at is None
    assert job.error == "Job with type 'install_server' must have server_id"


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


async def test_job_claim_waits_for_other_worker(world: World, worker: JobWorker) -> None:
    """Один id в очереди дважды, два воркера взяли его одновременно. Первый «на середине захвата» держит строку;
    второй обязан дождаться его commit, увидеть RUNNING и отказаться — а не прочитать PENDING и выполнить ещё раз."""
    server, _, fake = await world.server()
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.SYNC_SERVER, server_id=server.id)
    commands = len(fake.commands)
    async with session_factory() as first:
        claimed = await first.get(Job, job.id, with_for_update=True)  # первый воркер захватывает задачу
        assert claimed is not None
        claimed.status = JobStatus.RUNNING
        second = asyncio.create_task(worker.process_next(timeout=1))
        await asyncio.sleep(0.3)
        assert not second.done()  # второй ждёт блокировку строки, а не выполняет задачу
        await first.commit()
    assert await second
    assert len(fake.commands) == commands  # на сервер второй раз не пошли
    await world.session.refresh(job)
    assert job.status == JobStatus.RUNNING


async def test_requeue_rule_returns_lost_pending_jobs(world: World, worker: JobWorker) -> None:
    server, _, _ = await world.server()
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.HEALTH_CHECK, server_id=server.id)
    assert await requeue_lost_jobs(0) == "requeued 0 lost pending jobs"  # задача в очереди — не трогаем
    await get_redis().delete(QUEUE_KEY)  # Redis «потерял» очередь
    assert await requeue_lost_jobs(3600) == "requeued 0 lost pending jobs"  # ещё молодая — ждём
    assert await requeue_lost_jobs(0) == "requeued 1 lost pending jobs"
    assert await drain(worker) == 1
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS


async def test_retention_rule_deletes_only_old_finished_history(world: World, session: AsyncSession) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=10)
    old_done = Job(type=JobType.HEALTH_CHECK, status=JobStatus.SUCCESS, created_at=old)
    old_pending = Job(type=JobType.HEALTH_CHECK, status=JobStatus.PENDING, created_at=old)
    fresh_done = Job(type=JobType.HEALTH_CHECK, status=JobStatus.SUCCESS)
    old_log = AuditLog(action=AuditAction.LOGIN, created_at=old)
    fresh_log = AuditLog(action=AuditAction.LOGOUT)
    session.add_all([old_done, old_pending, fresh_done, old_log, fresh_log])
    await session.commit()

    assert await apply_retention(0, 0) == "deleted 0 finished jobs, 0 audit records"  # 0 — хранить вечно
    assert await apply_retention(7, 7) == "deleted 1 finished jobs, 1 audit records"
    session.expire_all()
    left_jobs = {job.id for job in await world.jobs(JobType.HEALTH_CHECK)}
    assert left_jobs == {old_pending.id, fresh_done.id}  # незавершённую не удаляем, даже старую
    assert [log.action for log in (await session.execute(select(AuditLog))).scalars()] == [AuditAction.LOGOUT]


async def test_expired_server_lock_does_not_fail_finished_work(world: World) -> None:
    """Задача шла дольше TTL блокировки: освобождение уже истёкшего ключа не должно превращать успех в FAILED."""
    server, _, _ = await world.server()
    async with ServerLock(server.id, timeout=1):
        await asyncio.sleep(1.3)  # ключ в Redis истёк, пока «шла» работа
    # дошли сюда без LockNotOwnedError
    assert ServerLock(server.id).timeout >= 1800  # по умолчанию TTL не короче самой долгой задачи
