"""Задача DISCOVER_VPN и автопоиск после установки сервера (этап 23.2): воркер настоящий, SSH фейковый.
Поиск только читает: на сервере ничего не меняется, в БД панели ничего не создаётся, секреты в отчёт не попадают."""

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import func, select

from core.exceptions import SSHConnectionError
from core.ssh_exception import CommandExecutionError
from infrastructure.vpn.awg_config import generate_secret_keys
from infrastructure.workers.locks import ServerLock
from infrastructure.workers.worker import JobWorker
from modules.access.models.vpn_credential import VPNCredential
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.jobs.service import JobService
from modules.protocols.repository import ProtocolRepository
from modules.servers.models.server import ServerStatus
from tests.fakes.fake_ssh import AppPeer
from tests.fixtures import World
from tests.integration.conftest import AdminLogin, drain
from tests.integration.discovery_support import (
    CLIENT_KEYS,
    NOW,
    OLD_HANDSHAKE,
    PROTOCOL_KEYS,
    add_app,
    assert_no_secrets,
    by_type,
    discover_action,
    discover_job_type,
    run_discovery,
)


def key() -> str:
    return generate_secret_keys().public_key


# --- результат задачи ---


async def test_discover_job_saves_found_amnezia_in_result(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    named, nameless, wide = key(), key(), key()
    app = add_app(
        fake,
        peers=[
            AppPeer(named, "10.8.1.2/32", handshake=NOW),
            AppPeer(nameless, "10.8.1.3/32", handshake=OLD_HANDSHAKE),
            AppPeer(wide, "10.8.1.4/32, 10.8.1.5/32"),
        ],
        clients_table={named: "Phone", wide: "Wide"},
        port=41234,
    )
    job = await run_discovery(world, worker, server)

    assert job.status == JobStatus.SUCCESS, job.error
    assert job.result is not None
    (protocol,) = job.result["protocols"]
    assert set(protocol) == PROTOCOL_KEYS
    assert (protocol["type"], protocol["container"], protocol["state"]) == (
        "amnezia_app_awg2",
        "amnezia-awg2",
        "running",
    )
    assert (protocol["image"], protocol["port"]) == (app.image, 41234)
    clients = {c["public_key"]: c for c in protocol["clients"]}
    assert all(set(c) == CLIENT_KEYS for c in protocol["clients"])
    assert [c["public_key"] for c in protocol["clients"]] == [named, nameless, wide]
    assert (clients[named]["address"], clients[named]["name"], clients[named]["supported"]) == (
        "10.8.1.2",
        "Phone",
        True,
    )
    assert datetime.fromisoformat(clients[named]["latest_handshake"]) == datetime.fromtimestamp(NOW, tz=timezone.utc)
    assert clients[nameless]["name"] is None and clients[nameless]["supported"] is True
    assert datetime.fromisoformat(clients[nameless]["latest_handshake"]) == datetime.fromtimestamp(
        OLD_HANDSHAKE, tz=timezone.utc
    )
    assert clients[wide]["supported"] is False and clients[wide]["latest_handshake"] is None
    assert job.progress == 100 and job.started_at and job.finished_at


async def test_discover_job_lists_every_found_app_container(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    add_app(fake, "amnezia-awg", peers=[key()])
    add_app(fake, "amnezia-awg2", peers=[key(), key()])
    job = await run_discovery(world, worker, server)
    assert job.status == JobStatus.SUCCESS and job.result is not None
    found = by_type(job.result)
    assert set(found) == {"amnezia_app_awg", "amnezia_app_awg2"}
    assert (found["amnezia_app_awg"]["container"], len(found["amnezia_app_awg"]["clients"])) == ("amnezia-awg", 1)
    assert (found["amnezia_app_awg2"]["container"], len(found["amnezia_app_awg2"]["clients"])) == ("amnezia-awg2", 2)
    assert [item["type"] for item in job.result["protocols"]] == ["amnezia_app_awg", "amnezia_app_awg2"]


async def test_discover_job_on_server_without_amnezia_has_empty_report(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    job = await run_discovery(world, worker, server)
    assert job.status == JobStatus.SUCCESS and job.result is not None
    assert job.result["protocols"] == []
    assert fake.changing_commands() == []


async def test_discover_job_ignores_the_protocol_installed_by_the_panel(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server()  # AMNEZIA_WG панели; в отчёт «найденной» Amnezia он не входит
    add_app(fake, "amnezia-awg2", peers=[key()])
    job = await run_discovery(world, worker, server)
    assert job.result is not None
    assert [item["type"] for item in job.result["protocols"]] == ["amnezia_app_awg2"]


async def test_discover_job_does_not_take_the_panel_container_for_the_app(world: World, worker: JobWorker) -> None:
    server, _, _ = await world.server()  # свой контейнер amnezia-awg, чужих нет
    job = await run_discovery(world, worker, server)
    assert job.status == JobStatus.SUCCESS and job.result is not None
    assert job.result["protocols"] == []


async def test_discover_job_reports_stopped_container_without_clients(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    app = add_app(fake, peers=[key()], running=False)
    job = await run_discovery(world, worker, server)
    assert job.status == JobStatus.SUCCESS and job.result is not None
    (protocol,) = job.result["protocols"]
    assert (protocol["state"], protocol["image"], protocol["clients"]) == ("stopped", app.image, [])


async def test_discover_job_result_has_no_secrets(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    k = key()
    v1 = add_app(fake, "amnezia-awg", peers=[key()])
    v2 = add_app(fake, "amnezia-awg2", peers=[k], clients_table={k: "Phone"})
    job = await run_discovery(world, worker, server)
    assert job.result is not None
    assert_no_secrets(job.result, v1, v2)
    assert_no_secrets({"error": job.error, "message": job.message}, v1, v2)


async def test_discover_job_changes_nothing_on_the_server_or_in_the_panel(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    k = key()
    add_app(fake, peers=[k], clients_table={k: "Phone"})
    files_before = {c: dict(app.files) for c, app in fake.apps.items()}
    job = await run_discovery(world, worker, server)
    assert job.status == JobStatus.SUCCESS
    assert fake.changing_commands() == [] and fake.writes == []
    assert {c: dict(app.files) for c, app in fake.apps.items()} == files_before
    assert fake.containers == {"amnezia-awg2": "running"}
    assert await ProtocolRepository(world.session).list_by_server_id(server.id) == []  # «найдено» ≠ «подключено»
    assert (await world.session.execute(select(func.count()).select_from(VPNCredential))).scalar_one() == 0


async def test_discover_job_twice_gives_the_same_report(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    add_app(fake, peers=[key(), key()])
    first = await run_discovery(world, worker, server)
    second = await run_discovery(world, worker, server)
    assert first.result == second.result and first.id != second.id


async def test_discover_job_runs_without_server_lock(world: World, worker: JobWorker) -> None:
    """Как health: только чтение, не ждёт выдачу и отзыв клиентов."""
    server, _, fake = await world.server(install=False)
    add_app(fake, peers=[key()])
    job = await JobService(world.session, SYSTEM_ACTOR).create(discover_job_type(), server_id=server.id)
    async with ServerLock(server.id, blocking_timeout=0):
        assert await worker.process_next(timeout=1)
        await world.session.refresh(job)
        assert job.status == JobStatus.SUCCESS  # не вернулась в очередь «сервер занят»
        assert await JobQueue().size() == 0


async def test_discover_job_without_server_id_is_rejected(world: World, worker: JobWorker) -> None:
    job = Job(type=discover_job_type(), server_id=None)
    world.session.add(job)
    await world.session.commit()
    await JobQueue().enqueue(job.id)
    assert await worker.process_next(timeout=1)
    await world.session.refresh(job)
    assert job.status == JobStatus.FAILED and job.started_at is None
    assert job.error == "Job with type 'discover_vpn' must have server_id"


# --- ошибки ---


async def test_discover_job_fails_when_server_is_unreachable(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    fake.reachable = False
    job = await run_discovery(world, worker, server)
    assert job.status == JobStatus.FAILED and "Connection error" in (job.error or "")
    assert job.result is None and job.finished_at is not None and job.progress == 0


async def test_discover_job_fails_when_ssh_breaks_midway(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    app = add_app(fake, peers=[key()])
    original = fake.run

    def breaking(command: str) -> Any:
        if command.startswith("docker exec"):
            raise SSHConnectionError("Connection lost while reading the container")
        return original(command)

    fake.run = breaking  # type: ignore[method-assign]
    job = await run_discovery(world, worker, server)
    assert job.status == JobStatus.FAILED and "Connection lost" in (job.error or "")
    assert job.result is None
    assert_no_secrets({"error": job.error}, app)
    assert fake.changing_commands() == []


async def test_discover_job_error_has_no_secrets_when_docker_fails(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    app = add_app(fake, peers=[key()])
    fake.ps_fails = True
    job = await run_discovery(world, worker, server)
    assert_no_secrets({"error": job.error, "result": job.result}, app)  # упал ли поиск или нет — текст чист


# --- аудит ---


async def test_discover_audit_started_when_queued_and_completed_with_counters(
    world: World, worker: JobWorker, admin: AdminLogin
) -> None:
    server, _, fake = await world.server(install=False)
    keys_v1, keys_v2 = [key(), key()], [key(), key(), key()]
    v1 = add_app(fake, "amnezia-awg", peers=keys_v1, clients_table={keys_v1[0]: "Secret Name One"})
    v2 = add_app(fake, "amnezia-awg2", peers=keys_v2, clients_table={keys_v2[0]: "Secret Name Two"})
    actor = AuditActor(admin_id=admin.admin.id)
    job = await JobService(world.session, actor).create(discover_job_type(), server_id=server.id)

    (started,) = await world.audit(discover_action("started"), server.id)
    assert started.admin_id == admin.admin.id and started.meta == {"job_id": str(job.id)}

    await drain(worker)
    (completed,) = await world.audit(discover_action("completed"), server.id)
    assert completed.admin_id == admin.admin.id and completed.entity_type == "server"
    meta = completed.meta or {}
    assert meta["job_id"] == str(job.id)
    counters = [value for name, value in meta.items() if name != "job_id"]
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in counters), meta
    assert {2, 5} <= set(counters)  # протоколов 2, клиентов 5
    text = json.dumps(meta)
    assert "Secret Name" not in text and not any(k in text for k in [*keys_v1, *keys_v2])  # ни имён, ни ключей
    assert_no_secrets(meta, v1, v2)
    assert await world.audit(discover_action("failed"), server.id) == []


async def test_discover_audit_failed_has_error_and_no_secrets(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    app = add_app(fake, peers=[key()])
    fake.reachable = False
    job = await run_discovery(world, worker, server)
    assert job.status == JobStatus.FAILED
    (failed,) = await world.audit(discover_action("failed"), server.id)
    assert "Connection error" in (failed.meta or {})["error"]
    assert await world.audit(discover_action("completed"), server.id) == []
    assert_no_secrets(failed.meta, app)


# --- автопоиск после установки сервера ---


async def install_job(world: World, worker: JobWorker, server_id: Any) -> Job:
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.INSTALL_SERVER, server_id=server_id)
    await drain(worker)
    await world.session.refresh(job)
    return job


async def test_install_server_job_saves_discovery_report_in_result(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    k = key()
    add_app(fake, peers=[k], clients_table={k: "Phone"})
    job = await install_job(world, worker, server.id)
    assert job.status == JobStatus.SUCCESS, job.error
    await world.session.refresh(server)
    assert server.status == ServerStatus.READY
    assert job.result is not None
    (protocol,) = job.result["protocols"]
    assert (protocol["type"], protocol["container"]) == ("amnezia_app_awg2", "amnezia-awg2")
    assert [(c["public_key"], c["name"]) for c in protocol["clients"]] == [(k, "Phone")]


async def test_install_server_job_on_clean_server_has_empty_discovery(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    job = await install_job(world, worker, server.id)
    assert job.status == JobStatus.SUCCESS
    assert job.result is not None and job.result["protocols"] == []  # отчёт есть и на чистом сервере
    assert fake.changing_commands() == []  # Docker уже есть, ставить нечего; поиск ничего не меняет


async def test_install_server_discovery_result_has_no_secrets(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    app = add_app(fake, peers=[key(), key()])
    job = await install_job(world, worker, server.id)
    assert job.result is not None and job.result["protocols"]  # поиск был, и отчёт сохранён
    assert_no_secrets(job.result, app)


async def test_install_server_job_succeeds_when_discovery_crashes(
    world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Поиск — приложение к установке: упал он — установка всё равно SUCCESS, сервер READY."""
    server, _, fake = await world.server(install=False)
    add_app(fake, peers=[key()])
    from infrastructure.vpn import amnezia_app

    async def boom(self: Any) -> None:
        raise CommandExecutionError("discovery blew up")

    monkeypatch.setattr(amnezia_app.AmneziaAppAWG2Provider, "discover", boom)
    job = await install_job(world, worker, server.id)
    assert job.status == JobStatus.SUCCESS and job.error is None
    await world.session.refresh(server)
    assert server.status == ServerStatus.READY
    assert not (job.result or {}).get("protocols")  # отчёта поиска нет, но и установка не пострадала


async def test_install_server_job_succeeds_when_docker_listing_fails(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    fake.ps_fails = True  # установщик `docker ps` не вызывает, поиск — вызывает
    job = await install_job(world, worker, server.id)
    assert job.status == JobStatus.SUCCESS, job.error
    await world.session.refresh(server)
    assert server.status == ServerStatus.READY
    assert any(c.startswith("docker ps") for c in fake.commands)  # поиск действительно пробовал и упал


async def test_install_server_failure_skips_discovery(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    fake.reachable = False
    job = await install_job(world, worker, server.id)
    assert job.status == JobStatus.FAILED and job.result is None
    await world.session.refresh(server)
    assert server.status == ServerStatus.ERROR


async def test_install_server_does_not_touch_the_app_container(world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    add_app(fake, peers=[key()])
    files_before = {c: dict(app.files) for c, app in fake.apps.items()}
    await install_job(world, worker, server.id)
    assert fake.container_reads, "discovery after the install did not read the app container"
    assert fake.changing_commands() == [] and fake.writes == []
    assert {c: dict(app.files) for c, app in fake.apps.items()} == files_before
    assert fake.containers == {"amnezia-awg2": "running"}
    assert await ProtocolRepository(world.session).list_by_server_id(server.id) == []
