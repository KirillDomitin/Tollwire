"""Чужой контейнер amnezia-awg (его поставило приложение AmneziaVPN) глазами задач воркера: установка протокола
падает понятной ошибкой, снятие протокола ничего не меняет на сервере. SSH — фейковый, воркер — настоящий."""

from infrastructure.vpn.config import CONTAINER_NAME
from infrastructure.workers.worker import JobWorker
from modules.audit.actor import SYSTEM_ACTOR
from modules.jobs.models.job import JobStatus, JobType
from modules.jobs.service import JobService
from modules.protocols.repository import ProtocolRepository
from tests.fixtures import World
from tests.integration.conftest import drain


async def test_install_protocol_job_fails_with_clear_error_on_foreign_container(
    world: World, worker: JobWorker
) -> None:
    server, _, fake = await world.server(install=False)
    fake.add_foreign_container("running")
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.INSTALL_PROTOCOL, server_id=server.id)

    await drain(worker)

    await world.session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert "AmneziaVPN" in (job.error or "") and CONTAINER_NAME in (job.error or "")  # админ видит причину
    assert await ProtocolRepository(world.session).list_by_server_id(server.id) == []  # протокол не «установлен»
    assert fake.mutating_commands() == [] and fake.writes == []  # чужой VPN не тронут
    assert fake.containers == {CONTAINER_NAME: "running"}


async def test_remove_protocol_job_leaves_foreign_container_alone(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    fake.add_foreign_container("running")
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.REMOVE_PROTOCOL, server_id=server.id)

    await drain(worker)

    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS  # снимать нечего: панель этот контейнер не ставила
    assert fake.mutating_commands() == [] and fake.writes == []
    assert fake.containers == {CONTAINER_NAME: "running"}
