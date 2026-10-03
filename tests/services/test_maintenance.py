"""Служебные правила планировщика и запросы под них: сессии, плановые проверки, зависшие задачи, аудит задач."""

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from core.security import hash_password
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.audit.models.audit_log import AuditAction
from modules.auth.models.admin import Admin
from modules.auth.models.admin_session import AdminSession
from modules.auth.service import AuthService
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.service import JobService
from modules.servers.maintenance import ServerMaintenanceService
from modules.servers.models.server import ServerStatus
from modules.servers.repository import ServerRepository
from tests.services.conftest import World


async def make_admin(world: World) -> Admin:
    admin = Admin(email=f"{secrets.token_hex(4)}@example.com", password_hash=hash_password("x"))
    world.session.add(admin)
    await world.session.commit()
    return admin


async def test_cleanup_deletes_only_expired_sessions(world: World) -> None:
    admin = await make_admin(world)
    now = datetime.now(timezone.utc)
    for expires_at in (now - timedelta(days=1), now - timedelta(seconds=1), now + timedelta(days=1)):
        world.session.add(AdminSession(admin_id=admin.id, token_hash=secrets.token_hex(32), expires_at=expires_at))
    await world.session.commit()
    assert await AuthService(world.session, SYSTEM_ACTOR).cleanup_expired_sessions() == 2
    left = (await world.session.execute(select(func.count()).select_from(AdminSession))).scalar_one()
    assert left == 1


async def test_checkable_servers(world: World) -> None:
    with_protocol, _, _ = await world.server()
    without_protocol, _, _ = await world.server(install=False)
    installing, _, _ = await world.server(install=False)
    installing.status = ServerStatus.INSTALLING
    await world.session.commit()
    repository = ServerRepository(world.session)
    assert set(await repository.list_checkable_ids(with_protocol=False)) == {with_protocol.id, without_protocol.id}
    assert set(await repository.list_checkable_ids(with_protocol=True)) == {with_protocol.id}


async def test_periodic_checks_skip_servers_with_unfinished_job(world: World) -> None:
    with_protocol, _, _ = await world.server()
    without_protocol, _, _ = await world.server(install=False)
    maintenance = ServerMaintenanceService(world.session)
    health = await maintenance.schedule_health_checks()
    assert {job.server_id for job in health} == {with_protocol.id, without_protocol.id}
    assert all(job.admin_id is None for job in health)  # плановые — от системы
    assert await maintenance.schedule_health_checks() == []  # предыдущие ещё ждут — не копим очередь
    assert [job.server_id for job in await maintenance.schedule_syncs()] == [with_protocol.id]


async def test_stale_running_jobs_are_failed(world: World) -> None:
    server, _, _ = await world.server()
    now = datetime.now(timezone.utc)
    stale = Job(
        type=JobType.REVOKE_ACCESS, server_id=server.id, status=JobStatus.RUNNING, started_at=now - timedelta(hours=2)
    )
    fresh = Job(type=JobType.SYNC_SERVER, server_id=server.id, status=JobStatus.RUNNING, started_at=now)
    world.session.add_all([stale, fresh])
    await world.session.commit()
    assert await JobService(world.session, SYSTEM_ACTOR).fail_stale_running(1800) == 1
    await world.session.refresh(stale)
    await world.session.refresh(fresh)
    assert stale.status == JobStatus.FAILED and stale.finished_at is not None
    assert fresh.status == JobStatus.RUNNING
    (failed,) = await world.audit(AuditAction.CREDENTIAL_REVOKE_FAILED)
    assert failed.meta is not None and failed.meta["job_id"] == str(stale.id)


async def test_job_start_and_finish_are_audited_for_initiator(world: World) -> None:
    server, _, _ = await world.server()
    admin = await make_admin(world)
    jobs = JobService(world.session, AuditActor(admin_id=admin.id, ip="198.51.100.7"))
    job = await jobs.create(JobType.INSTALL_SERVER, server_id=server.id)
    assert job.admin_id == admin.id
    (started,) = await world.audit(AuditAction.SERVER_INSTALL_STARTED)
    assert (started.admin_id, started.ip_address, started.entity_id) == (admin.id, "198.51.100.7", server.id)

    job.status = JobStatus.FAILED
    job.error = "boom"
    JobService(world.session, AuditActor(admin_id=job.admin_id)).record_finished(job)  # как делает воркер
    await world.session.commit()
    (failed,) = await world.audit(AuditAction.SERVER_INSTALL_FAILED)
    assert failed.admin_id == admin.id and failed.ip_address is None and failed.meta["error"] == "boom"  # type: ignore[index]
