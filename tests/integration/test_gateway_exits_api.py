"""Выходы шлюза (этап 24.2) через HTTP и воркер: добавление в две фазы, снятие, PATCH, GET без секретов, защиты 409,
аудит, sync выходов и шлюза. Два-три FakeServer одновременно: шлюз (раскладка GATEWAY_LAYOUT) и выходы (обычная)."""

import inspect
import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from core.database.postgres import session_factory
from core.exceptions import ServerBusyError
from core.security import decrypt_secret
from infrastructure.vpn.awg_config import list_peers, remove_peer
from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT, exit_host_file
from infrastructure.vpn.provider import VPNProvider
from infrastructure.workers import worker as worker_module
from infrastructure.workers.locks import ServerLock
from infrastructure.workers.worker import JobWorker
from modules.gateway import repository as gateway_repository
from modules.gateway import service as gateway_service
from modules.gateway.models.gateway_exit import ExitState, GatewayExit
from modules.gateway.naming import exit_client_name
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.servers.models.server import VPNServer
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient
from tests.fixtures import World
from tests.gateway_support import fresh_handshake
from tests.integration.conftest import drain
from tests.integration.test_gateway_api import gateway_server, install_gateway, protocols
from tests.integration.test_mvp_flow import run_job

GW_DIR = GATEWAY_LAYOUT.host_config_dir
LEAK_FIELDS = ("client_public_key", "client_address", "encrypted_config", "PrivateKey", "PresharedKey")


def exits_url(sid: UUID | str, exit_id: UUID | str | None = None) -> str:
    base = f"/api/v1/servers/{sid}/gateway/exits"
    return base if exit_id is None else f"{base}/{exit_id}"


async def exit_server(world: World, host: str) -> tuple[VPNServer, VPNProtocol, FakeServer]:
    """Сервер с обычным протоколом (AMNEZIA_WG) и своим адресом: из него строится Endpoint туннеля."""
    server, protocol, fake = await world.server()
    assert protocol is not None
    server.host = fake.host = host
    await world.session.commit()
    return server, protocol, fake


class Scene:
    def __init__(self, sid: UUID, gateway: FakeServer, gateway_protocol_id: UUID) -> None:
        self.sid, self.gateway, self.gateway_protocol_id = sid, gateway, gateway_protocol_id


async def scene(api: httpx.AsyncClient, worker: JobWorker, world: World) -> Scene:
    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)
    (protocol,) = await protocols(api, sid)
    return Scene(sid, fake, UUID(protocol["id"]))


async def post_exit(api: httpx.AsyncClient, sid: UUID, exit_server_id: UUID, **extra: Any) -> httpx.Response:
    return await api.post(exits_url(sid), json={"exit_server_id": str(exit_server_id), **extra})


async def list_exits(api: httpx.AsyncClient, sid: UUID) -> list[dict[str, Any]]:
    response = await api.get(exits_url(sid))
    assert response.status_code == 200, response.text
    return list(response.json())


async def add_exit(
    api: httpx.AsyncClient, worker: JobWorker, sid: UUID, exit_server_id: UUID, **extra: Any
) -> dict[str, Any]:
    """POST + воркер до конца; возвращает выход из GET (задача должна завершиться успешно)."""
    job = await run_job(api, worker, await post_exit(api, sid, exit_server_id, **extra))
    assert job["status"] == "success", job
    (found,) = [item for item in await list_exits(api, sid) if item["exit_server_id"] == str(exit_server_id)]
    return found


async def db_exit(exit_id: str | UUID) -> GatewayExit:
    async with session_factory() as session:
        row = await session.get(GatewayExit, UUID(str(exit_id)))
        assert row is not None
        return row


async def db_exits() -> list[GatewayExit]:
    async with session_factory() as session:
        return list((await session.execute(select(GatewayExit).order_by(GatewayExit.slot))).scalars())


def peer_names(fake: FakeServer) -> list[str | None]:
    return [peer.name for peer in list_peers(fake.files[fake.config_file])]


# --- POST: постановка ---------------------------------------------------------------------------------------------


async def test_post_queues_a_job_creates_a_pending_row_and_hides_the_params(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")

    response = await post_exit(api, gw.sid, server.id, weight=7, max_clients=40)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["type"] == "gateway_add_exit" and body["server_id"] == str(gw.sid) and "params" not in body
    (row,) = await db_exits()
    assert (row.slot, row.weight, row.max_clients, row.state) == (1, 7, 40, ExitState.PENDING)
    job = await world.session.get(Job, UUID(body["id"]))
    assert job is not None and job.params == {
        "exit_id": str(row.id),
        "exit_server_id": str(server.id),
        "slot": 1,
        "weight": 7,
    }
    (listed,) = await list_exits(api, gw.sid)
    assert listed["state"] == "pending" and listed["slot"] == 1 and listed["exit_server_name"] == server.name


async def test_slots_are_the_smallest_free_ones_and_are_reused(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    servers = [await exit_server(world, f"198.51.100.{20 + index}") for index in range(3)]
    added = [await add_exit(api, worker, gw.sid, server.id) for server, _, _ in servers]
    assert [item["slot"] for item in added] == [1, 2, 3]

    removed = await run_job(api, worker, await api.delete(exits_url(gw.sid, added[1]["id"])))
    assert removed["status"] == "success", removed
    again = await add_exit(api, worker, gw.sid, servers[1][0].id)
    assert again["slot"] == 2  # освободившийся слот, а не четвёртый
    assert [item["slot"] for item in await list_exits(api, gw.sid)] == [1, 2, 3]


async def test_a_full_gateway_has_no_slot_left(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_repository, "MAX_SLOT", 1)
    gw = await scene(api, worker, world)
    first, second = await exit_server(world, "198.51.100.20"), await exit_server(world, "198.51.100.21")
    await add_exit(api, worker, gw.sid, first[0].id)
    response = await post_exit(api, gw.sid, second[0].id)
    assert response.status_code == 409 and "slot" in response.json()["detail"]


async def test_post_checks_return_404_409_and_422_without_creating_anything(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    plain, _, _ = await world.server(install=False)  # SSH есть, протокола нет
    no_creds = (await api.post("/api/v1/servers", json={"name": "bare", "host": "192.0.2.5"})).json()["id"]
    gateway_too, _, _ = await world.server(install=False)
    world.fakes[gateway_too.id] = FakeServer(host=gateway_too.host, layout=GATEWAY_LAYOUT)
    await install_gateway(api, worker, gateway_too.id)

    unknown = "00000000-0000-4000-8000-000000000000"
    cases = {
        "no such server": (await post_exit(api, UUID(unknown), server.id), 404),
        "no such exit server": (await post_exit(api, gw.sid, UUID(unknown)), 404),
        "gateway not installed": (await post_exit(api, plain.id, server.id), 409),
        "gateway without credentials": (await post_exit(api, UUID(no_creds), server.id), 409),
        "exit is the gateway": (await post_exit(api, gw.sid, gw.sid), 422),
        "exit has no protocol": (await post_exit(api, gw.sid, plain.id), 409),
        "exit without credentials": (await post_exit(api, gw.sid, UUID(no_creds)), 409),
        "exit is a gateway": (await post_exit(api, gw.sid, gateway_too.id), 409),
        "weight 0": (await post_exit(api, gw.sid, server.id, weight=0), 422),
        "weight 101": (await post_exit(api, gw.sid, server.id, weight=101), 422),
        "max_clients 0": (await post_exit(api, gw.sid, server.id, max_clients=0), 422),
        "weight text": (await post_exit(api, gw.sid, server.id, weight="x"), 422),
        "no body": (await api.post(exits_url(gw.sid), json={}), 422),
        "bad id": (await api.post(exits_url(gw.sid), json={"exit_server_id": "nope"}), 422),
    }
    wrong = {name: response.status_code for name, (response, code) in cases.items() if response.status_code != code}
    assert wrong == {}
    assert await db_exits() == [] and await world.jobs(JobType.GATEWAY_ADD_EXIT) == []


async def test_a_regular_wg_server_with_a_gateway_protocol_is_refused_as_an_exit_but_app_protocols_do_not_count(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    app_only, _, _ = await world.server(install=False)
    world.session.add(VPNProtocol(server_id=app_only.id, type=ProtocolType.AMNEZIA_APP_AWG2, port=1))
    await world.session.commit()
    response = await post_exit(api, gw.sid, app_only.id)
    assert response.status_code == 409 and "AmneziaWG" in response.json()["detail"]


async def test_adding_an_exit_twice_is_refused_while_a_job_is_unfinished_and_after_it_succeeded(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    assert (await post_exit(api, gw.sid, server.id)).status_code == 202
    assert (await post_exit(api, gw.sid, server.id)).status_code == 409  # задача ещё в очереди
    assert len(await db_exits()) == 1
    await drain(worker)
    response = await post_exit(api, gw.sid, server.id)
    assert response.status_code == 409 and "already" in response.json()["detail"]
    assert len(await world.jobs(JobType.GATEWAY_ADD_EXIT)) == 1


async def test_two_gateways_may_share_one_exit(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    first, second = await scene(api, worker, world), await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    await add_exit(api, worker, first.sid, server.id)
    await add_exit(api, worker, second.sid, server.id)
    assert len([name for name in peer_names(fake) if name and name.startswith("gw-")]) == 2


# --- воркер: две фазы ---------------------------------------------------------------------------------------------


async def test_worker_creates_the_internal_client_then_the_tunnel_and_activates_the_exit(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, protocol, fake = await exit_server(world, "198.51.100.20")

    exit_ = await add_exit(api, worker, gw.sid, server.id)

    row = await db_exit(exit_["id"])
    assert row.state == ExitState.ACTIVE and row.client_public_key and row.client_address
    assert peer_names(fake) == [exit_client_name(row.id)]
    assert row.client_public_key in fake.config_peer_keys()
    config = decrypt_secret(row.encrypted_config or "")
    assert row.encrypted_config and "PrivateKey" not in row.encrypted_config  # хранится только шифртекст
    tunnel_file = gw.gateway.files[exit_host_file(GATEWAY_LAYOUT, 1)]
    assert f"Endpoint = 198.51.100.20:{protocol.port}\n" in tunnel_file
    assert config.splitlines()[1] in tunnel_file  # PrivateKey внутреннего клиента
    assert gw.gateway.exit_interfaces == ["gwx1"]
    job = (await world.jobs(JobType.GATEWAY_ADD_EXIT))[0]
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS
    assert job.result == {"exit_id": exit_["id"], "exit_server_id": str(server.id), "slot": 1}


async def test_locks_are_taken_gateway_first_and_the_exit_inside(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    events: list[tuple[str, UUID]] = []

    class RecordingLock(ServerLock):
        async def __aenter__(self) -> Any:
            lock = await super().__aenter__()
            events.append(("enter", self.server_id))
            return lock

        async def __aexit__(self, *args: Any) -> None:
            events.append(("exit", self.server_id))
            await super().__aexit__(*args)

    monkeypatch.setattr(worker_module, "ServerLock", RecordingLock)
    monkeypatch.setattr(gateway_service, "ServerLock", RecordingLock)
    await add_exit(api, worker, gw.sid, server.id)
    assert events == [("enter", gw.sid), ("enter", server.id), ("exit", server.id), ("exit", gw.sid)]


async def test_a_failure_between_the_phases_keeps_the_internal_client_and_a_retry_does_not_duplicate_it(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    gw.gateway.exit_up_fails = {1}

    job = await run_job(api, worker, await post_exit(api, gw.sid, server.id))

    assert job["status"] == "failed" and "gwx1" in job["error"]
    (row,) = await db_exits()
    assert row.state == ExitState.PEER_CREATED and row.client_public_key  # фаза 1 сохранена
    assert len(peer_names(fake)) == 1 and gw.gateway.exit_interfaces == []
    assert exit_host_file(GATEWAY_LAYOUT, 1) not in gw.gateway.files  # файл без интерфейса не остался
    first_key = row.client_public_key

    gw.gateway.exit_up_fails = set()
    retry = await run_job(api, worker, await post_exit(api, gw.sid, server.id))  # то же слот и строка

    assert retry["status"] == "success", retry
    (row,) = await db_exits()
    assert row.state == ExitState.ACTIVE and row.slot == 1 and row.client_public_key == first_key
    assert len(peer_names(fake)) == 1  # второго peer'а на выходе нет
    assert gw.gateway.exit_interfaces == ["gwx1"]
    assert "created" not in str(fake.mutating_commands()[-1:])


async def test_a_retry_of_the_job_itself_after_the_first_phase_runs_only_the_second(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    gw.gateway.exit_up_fails = {1}
    failed = await run_job(api, worker, await post_exit(api, gw.sid, server.id))
    assert failed["status"] == "failed"
    gw.gateway.exit_up_fails = set()
    writes_on_exit = len(fake.writes)

    job = Job(
        type=JobType.GATEWAY_ADD_EXIT,
        server_id=gw.sid,
        params={"exit_id": str((await db_exits())[0].id)},
    )
    world.session.add(job)
    await world.session.commit()
    await worker.job_queue.enqueue(job.id)
    await drain(worker)

    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS
    assert len(fake.writes) == writes_on_exit  # выход не трогали: фаза 1 не повторялась
    assert (await db_exits())[0].state == ExitState.ACTIVE


async def test_an_internal_client_left_by_an_interrupted_first_phase_is_replaced_not_duplicated(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    """Клиент создан на выходе, а commit строки не дошёл: в строке нет ключа. Повтор находит клиента по имени."""
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    assert (await post_exit(api, gw.sid, server.id)).status_code == 202
    (row,) = await db_exits()
    async with FakeSSHClient(fake) as ssh:
        provider: VPNProvider = worker_module_provider(ssh)
        orphan = await provider.create_client(exit_client_name(row.id))

    await drain(worker)

    names = peer_names(fake)
    assert names == [exit_client_name(row.id)]  # один peer с этим именем
    (row,) = await db_exits()
    assert row.state == ExitState.ACTIVE and row.client_public_key != orphan.public_key
    assert orphan.public_key not in fake.config_peer_keys()


def worker_module_provider(ssh: Any) -> VPNProvider:
    from infrastructure.vpn.amnezia import AmneziaWGProvider

    return AmneziaWGProvider(ssh)


async def test_a_busy_exit_requeues_the_job_and_the_next_attempt_finishes_it(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")

    class QuickLock(ServerLock):
        def __init__(self, server_id: UUID) -> None:
            super().__init__(server_id, blocking_timeout=1)

    monkeypatch.setattr(gateway_service, "ServerLock", QuickLock)
    queued = (await post_exit(api, gw.sid, server.id)).json()
    async with ServerLock(server.id):  # выход занят другой задачей
        assert await worker.process_next(timeout=1) is True
        job = await world.session.get(Job, UUID(queued["id"]))
        assert job is not None
        await world.session.refresh(job)
        assert job.status == JobStatus.PENDING and job.error is None  # вернулась в очередь, а не упала
        assert (await db_exits())[0].state == ExitState.PENDING
        assert gw.gateway.exit_interfaces == [] and peer_names(fake) == []

    await drain(worker)
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS and (await db_exits())[0].state == ExitState.ACTIVE
    assert len(peer_names(fake)) == 1


async def test_a_failing_exit_server_fails_the_job_in_phase_one_and_leaves_a_pending_row(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    fake.reachable = False

    job = await run_job(api, worker, await post_exit(api, gw.sid, server.id))

    assert job["status"] == "failed"
    (row,) = await db_exits()
    assert row.state == ExitState.PENDING and row.client_public_key is None
    assert gw.gateway.exit_interfaces == [] and exit_host_file(GATEWAY_LAYOUT, 1) not in gw.gateway.files


async def test_secrets_do_not_reach_the_job_the_audit_or_the_api(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    gw.gateway.exit_up_fails = {1}
    failed = await run_job(api, worker, await post_exit(api, gw.sid, server.id))
    gw.gateway.exit_up_fails = set()
    ok = await run_job(api, worker, await post_exit(api, gw.sid, server.id))
    assert failed["status"] == "failed" and ok["status"] == "success"

    (row,) = await db_exits()
    secret_lines = [
        line.split("= ", 1)[1]
        for line in decrypt_secret(row.encrypted_config or "").splitlines()
        if line.startswith(("PrivateKey", "PresharedKey"))
    ]
    assert len(secret_lines) == 2
    audit = (await api.get("/api/v1/audit-logs", params={"limit": 100})).json()
    texts = [json.dumps(item) for item in (failed, ok, audit, await list_exits(api, gw.sid))]
    for text in texts:
        assert not any(secret in text for secret in secret_lines)
        assert not any(field in text for field in LEAK_FIELDS)


# --- аудит ---------------------------------------------------------------------------------------------------------


async def test_audit_of_add_started_completed_and_failed_carries_ids_slot_and_weight_only(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    gw.gateway.exit_up_fails = {1}
    await run_job(api, worker, await post_exit(api, gw.sid, server.id, weight=3))
    gw.gateway.exit_up_fails = set()
    await run_job(api, worker, await post_exit(api, gw.sid, server.id, weight=3))

    (row,) = await db_exits()
    expected = {"exit_id": str(row.id), "exit_server_id": str(server.id), "slot": 1, "weight": 3}
    from modules.audit.models.audit_log import AuditAction

    started = await world.audit(AuditAction.GATEWAY_EXIT_ADD_STARTED, gw.sid)
    completed = await world.audit(AuditAction.GATEWAY_EXIT_ADD_COMPLETED, gw.sid)
    failed = await world.audit(AuditAction.GATEWAY_EXIT_ADD_FAILED, gw.sid)
    assert len(started) == 2 and len(completed) == 1 and len(failed) == 1
    for entry in (*started, *completed, *failed):
        assert expected.items() <= (entry.meta or {}).items()
        assert set(entry.meta or {}) <= {*expected, "job_id", "error"}
        assert entry.entity_type == "server" and entry.admin_id is not None
    assert "gwx1" in failed[0].meta["error"]


async def test_audit_of_remove_started_and_completed(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    from modules.audit.models.audit_log import AuditAction

    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    await run_job(api, worker, await api.delete(exits_url(gw.sid, exit_["id"])))

    expected = {"exit_id": exit_["id"], "exit_server_id": str(server.id), "slot": 1}
    for action in (AuditAction.GATEWAY_EXIT_REMOVE_STARTED, AuditAction.GATEWAY_EXIT_REMOVE_COMPLETED):
        (entry,) = await world.audit(action, gw.sid)
        assert expected.items() <= (entry.meta or {}).items() and "weight" not in (entry.meta or {})


# --- GET ------------------------------------------------------------------------------------------------------------


async def test_get_lists_exits_by_slot_with_server_names_and_no_secrets(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    a, b = await exit_server(world, "198.51.100.20"), await exit_server(world, "198.51.100.21")
    await add_exit(api, worker, gw.sid, a[0].id, weight=2)
    await add_exit(api, worker, gw.sid, b[0].id, max_clients=10)

    response = await api.get(exits_url(gw.sid))

    items = response.json()
    assert [item["slot"] for item in items] == [1, 2]
    assert [item["exit_server_name"] for item in items] == [a[0].name, b[0].name]
    assert set(items[0]) == {
        "id",
        "exit_server_id",
        "exit_server_name",
        "slot",
        "state",
        "health",
        "last_handshake_at",
        "checked_at",
        "weight",
        "max_clients",
    }
    assert (items[0]["weight"], items[0]["max_clients"], items[1]["weight"], items[1]["max_clients"]) == (
        2,
        None,
        1,
        10,
    )
    assert items[0]["state"] == "active" and items[0]["health"] is None  # сверки ещё не было
    assert not any(field in response.text for field in LEAK_FIELDS)


async def test_get_of_a_server_without_a_gateway_is_empty_and_of_a_missing_server_is_404(
    api: httpx.AsyncClient, world: World
) -> None:
    plain, _, _ = await world.server(install=False)
    assert (await api.get(exits_url(plain.id))).json() == []
    assert (await api.get(exits_url("00000000-0000-4000-8000-000000000000"))).status_code == 404


# --- PATCH ----------------------------------------------------------------------------------------------------------


async def test_patch_changes_weight_and_limit_in_the_db_with_an_audit_and_no_job(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    from modules.audit.models.audit_log import AuditAction

    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id, max_clients=5)
    jobs_before = len(await world.jobs(JobType.GATEWAY_ADD_EXIT)) + len(await world.jobs(JobType.GATEWAY_REMOVE_EXIT))
    commands = len(gw.gateway.commands)

    response = await api.patch(exits_url(gw.sid, exit_["id"]), json={"weight": 9, "max_clients": 50})
    assert response.status_code == 200, response.text
    assert (response.json()["weight"], response.json()["max_clients"]) == (9, 50)
    cleared = await api.patch(exits_url(gw.sid, exit_["id"]), json={"max_clients": None})
    assert cleared.json()["max_clients"] is None and cleared.json()["weight"] == 9  # вес не тронут

    row = await db_exit(exit_["id"])
    assert (row.weight, row.max_clients) == (9, None)
    updates = await world.audit(AuditAction.GATEWAY_EXIT_UPDATED, gw.sid)
    changes = sorted(((entry.meta or {})["changes"] for entry in updates), key=len)
    assert changes == [{"max_clients": [50, None]}, {"weight": [1, 9], "max_clients": [5, 50]}]
    assert all(entry.meta["slot"] == 1 and entry.meta["exit_id"] == exit_["id"] for entry in updates)
    assert len(gw.gateway.commands) == commands  # SSH не открывался
    assert jobs_before == len(await world.jobs(JobType.GATEWAY_ADD_EXIT)) + len(
        await world.jobs(JobType.GATEWAY_REMOVE_EXIT)
    )


async def test_patch_without_changes_writes_no_audit_and_bad_values_are_422(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    from modules.audit.models.audit_log import AuditAction

    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    url = exits_url(gw.sid, exit_["id"])
    assert (await api.patch(url, json={})).status_code == 200
    assert (await api.patch(url, json={"weight": 1})).status_code == 200  # то же значение
    assert await world.audit(AuditAction.GATEWAY_EXIT_UPDATED) == []
    for body in ({"weight": 0}, {"weight": 101}, {"weight": None}, {"max_clients": 0}, {"weight": "a"}):
        assert (await api.patch(url, json=body)).status_code == 422, body


async def test_patch_of_an_unknown_exit_or_an_exit_of_another_gateway_is_404(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    first, second = await scene(api, worker, world), await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, first.sid, server.id)
    unknown = "00000000-0000-4000-8000-000000000000"
    assert (await api.patch(exits_url(first.sid, unknown), json={"weight": 2})).status_code == 404
    assert (await api.patch(exits_url(second.sid, exit_["id"]), json={"weight": 2})).status_code == 404
    assert (await api.delete(exits_url(second.sid, exit_["id"]))).status_code == 404
    assert (await db_exit(exit_["id"])).weight == 1


# --- DELETE: снятие --------------------------------------------------------------------------------------------------


async def test_delete_drains_at_once_then_the_worker_removes_the_tunnel_the_internal_client_and_the_row(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)

    response = await api.delete(exits_url(gw.sid, exit_["id"]))

    assert response.status_code == 202 and response.json()["type"] == "gateway_remove_exit"
    assert (await list_exits(api, gw.sid))[0]["state"] == "draining"  # новых клиентов на выход уже не назначают
    await drain(worker)
    assert await list_exits(api, gw.sid) == [] and await db_exits() == []
    assert gw.gateway.exit_interfaces == [] and exit_host_file(GATEWAY_LAYOUT, 1) not in gw.gateway.files
    assert peer_names(fake) == [] and fake.interface_peers == set()
    job = await world.session.get(Job, UUID(response.json()["id"]))
    assert job is not None
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS
    assert job.result == {
        "exit_id": exit_["id"],
        "slot": 1,
        "peer_revoked": True,
        "exit_server_id": str(server.id),
    }


async def test_removal_with_an_unreachable_exit_still_removes_the_tunnel_and_leaves_the_peer_to_sync(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    fake.reachable = False

    job = await run_job(api, worker, await api.delete(exits_url(gw.sid, exit_["id"])))

    assert job["status"] == "success" and job["result"]["peer_revoked"] is False
    assert await db_exits() == [] and gw.gateway.exit_interfaces == []
    assert len(peer_names(fake)) == 1  # peer остался на недоступном выходе
    fake.reachable = True
    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/sync"))
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_wg")
    assert len(report["removed_orphans"]) == 1 and peer_names(fake) == []


async def test_removal_of_a_half_added_exit_works_in_every_state(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    pending, _, pending_fake = await exit_server(world, "198.51.100.20")
    half, _, half_fake = await exit_server(world, "198.51.100.21")
    pending_fake.reachable = False
    await run_job(api, worker, await post_exit(api, gw.sid, pending.id))  # фаза 1 не прошла: PENDING
    gw.gateway.exit_up_fails = {2}
    await run_job(api, worker, await post_exit(api, gw.sid, half.id))  # PEER_CREATED, туннеля нет
    assert [row.state for row in await db_exits()] == [ExitState.PENDING, ExitState.PEER_CREATED]

    for item in await list_exits(api, gw.sid):
        job = await run_job(api, worker, await api.delete(exits_url(gw.sid, item["id"])))
        assert job["status"] == "success", job
    assert await db_exits() == [] and peer_names(half_fake) == []


async def test_a_failed_removal_keeps_the_row_and_the_internal_client_and_can_be_repeated(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    gw.gateway.exit_down_fails = {1}

    failed = await run_job(api, worker, await api.delete(exits_url(gw.sid, exit_["id"])))

    assert failed["status"] == "failed"
    (row,) = await db_exits()
    assert row.state == ExitState.REMOVING and len(peer_names(fake)) == 1  # клиент на выходе пока цел
    assert gw.gateway.exit_interfaces == ["gwx1"]
    gw.gateway.exit_down_fails = set()

    again = await run_job(api, worker, await api.delete(exits_url(gw.sid, exit_["id"])))

    assert again["status"] == "success", again
    assert await db_exits() == [] and gw.gateway.exit_interfaces == [] and peer_names(fake) == []


async def test_delete_is_refused_while_a_job_of_the_exit_is_unfinished(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    assert (await post_exit(api, gw.sid, server.id)).status_code == 202
    (row,) = await db_exits()
    assert (await api.delete(exits_url(gw.sid, row.id))).status_code == 409
    await drain(worker)
    assert (await api.delete(exits_url(gw.sid, row.id))).status_code == 202
    assert (await api.delete(exits_url(gw.sid, row.id))).status_code == 409  # снятие уже в очереди


async def test_remove_job_of_an_already_removed_exit_succeeds_without_touching_anything(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    job = Job(
        type=JobType.GATEWAY_REMOVE_EXIT, server_id=gw.sid, params={"exit_id": "00000000-0000-4000-8000-000000000001"}
    )
    world.session.add(job)
    await world.session.commit()
    await worker.job_queue.enqueue(job.id)
    commands = len(gw.gateway.commands)
    await drain(worker)
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS and job.result == {
        "exit_id": "00000000-0000-4000-8000-000000000001",
        "already_removed": True,
    }
    assert len(gw.gateway.commands) == commands


async def test_a_job_with_broken_params_fails_cleanly(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw = await scene(api, worker, world)
    for params in (None, {}, {"exit_id": "not-a-uuid"}):
        job = Job(type=JobType.GATEWAY_ADD_EXIT, server_id=gw.sid, params=params)
        world.session.add(job)
        await world.session.commit()
        await worker.job_queue.enqueue(job.id)
        await drain(worker)
        await world.session.refresh(job)
        assert job.status == JobStatus.FAILED and "invalid params" in (job.error or "")


async def test_the_new_job_types_need_a_server(world: World, worker: JobWorker) -> None:
    for job_type in (JobType.GATEWAY_ADD_EXIT, JobType.GATEWAY_REMOVE_EXIT):
        job = Job(type=job_type, server_id=None, params={"exit_id": "00000000-0000-4000-8000-000000000001"})
        world.session.add(job)
        await world.session.commit()
        await worker.job_queue.enqueue(job.id)
        await drain(worker)
        await world.session.refresh(job)
        assert job.status == JobStatus.FAILED and "server_id" in (job.error or "")


# --- защиты 409 и каскады ------------------------------------------------------------------------------------------


async def test_an_exit_server_cannot_be_deleted_nor_lose_its_wg_protocol_until_the_exit_is_removed(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)

    deleted = await api.delete(f"/api/v1/servers/{server.id}")
    removed = await api.request("DELETE", f"/api/v1/servers/{server.id}/protocol")
    assert deleted.status_code == 409 and "exit of a gateway" in deleted.json()["detail"]
    assert removed.status_code == 409 and "exit of a gateway" in removed.json()["detail"]
    assert await world.jobs(JobType.REMOVE_PROTOCOL) == []
    assert (await api.get(f"/api/v1/servers/{server.id}")).status_code == 200

    await run_job(api, worker, await api.delete(exits_url(gw.sid, exit_["id"])))

    job = await run_job(api, worker, await api.request("DELETE", f"/api/v1/servers/{server.id}/protocol"))
    assert job["status"] == "success"
    assert (await api.delete(f"/api/v1/servers/{server.id}")).status_code == 204


async def test_the_worker_rechecks_the_exit_before_removing_the_wg_protocol(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    """Выход подключили между постановкой задачи снятия протокола и её выполнением: контейнер не трогаем."""
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    queued = await api.request("DELETE", f"/api/v1/servers/{server.id}/protocol")
    assert queued.status_code == 202
    assert (await post_exit(api, gw.sid, server.id)).status_code == 202
    commands = len(fake.commands)
    await drain(worker)
    removal = await world.session.get(Job, UUID(queued.json()["id"]))
    assert removal is not None
    await world.session.refresh(removal)
    assert removal.status == JobStatus.FAILED and "exit of a gateway" in (removal.error or "")
    assert fake.containers.get("amnezia-awg") == "running"
    assert not any("docker stop" in command for command in fake.commands[commands:])


async def test_deleting_the_gateway_server_drops_its_exit_rows_and_sync_of_the_exit_removes_the_leftover_peer(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    await add_exit(api, worker, gw.sid, server.id)

    assert (await api.delete(f"/api/v1/servers/{gw.sid}")).status_code == 204

    assert await db_exits() == []
    assert len(peer_names(fake)) == 1  # peer gw-* остался на выходе
    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/sync"))
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_wg")
    assert len(report["removed_orphans"]) == 1 and peer_names(fake) == []


async def test_removing_the_gateway_protocol_drops_its_exit_rows(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    await add_exit(api, worker, gw.sid, server.id)
    job = await run_job(
        api, worker, await api.request("DELETE", f"/api/v1/servers/{gw.sid}/protocol", json={"type": "amnezia_gateway"})
    )
    assert job["status"] == "success", job
    assert await db_exits() == []
    assert (await api.delete(f"/api/v1/servers/{server.id}")).status_code == 204  # выход больше не держит сервер


# --- sync выхода: внутренние клиенты gw-* ---


async def test_sync_of_the_exit_returns_a_lost_internal_client_from_the_stored_config(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    row = await db_exit(exit_["id"])
    fake.files[fake.config_file], _ = remove_peer(fake.files[fake.config_file], row.client_public_key or "")
    fake.interface_peers = fake.config_peer_keys()
    assert peer_names(fake) == []

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/sync"))

    assert sync["status"] == "success", sync
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_wg")
    assert report["restored"] == [
        {"exit_id": exit_["id"], "address": row.client_address, "public_key": row.client_public_key}
    ]
    assert peer_names(fake) == [exit_client_name(row.id)] and row.client_public_key in fake.interface_peers
    assert report["removed_orphans"] == [] and report["issues"] == []


async def test_sync_of_the_exit_removes_an_orphan_gw_peer_and_keeps_live_foreign_and_grant_peers(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, protocol, fake = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    live = await db_exit(exit_["id"])
    _, _, _, credential = await world.issued(protocol)
    async with FakeSSHClient(fake) as ssh:
        provider = worker_module_provider(ssh)
        orphan = await provider.create_client("gw-aaaaaaaaaaaa")
        foreign = await provider.create_client("manual-peer")
        stranger = await provider.create_client("gw")  # имя без дефиса и цифр: не наш префикс

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_wg")
    assert [item["public_key"] for item in report["removed_orphans"]] == [orphan.public_key]
    assert {item["public_key"] for item in report["ok"]} == {live.client_public_key, credential.client_public_key}
    assert {item["public_key"] for item in report["foreign"]} == {foreign.public_key, stranger.public_key}
    assert orphan.public_key not in fake.config_peer_keys() and live.client_public_key in fake.config_peer_keys()
    assert foreign.public_key in fake.config_peer_keys() and stranger.public_key in fake.config_peer_keys()


async def test_sync_removes_the_peer_of_an_exit_that_is_being_removed(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    async with session_factory() as session:
        row = await session.get(GatewayExit, UUID(exit_["id"]))
        assert row is not None
        row.state = ExitState.REMOVING
        await session.commit()

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_wg")
    assert len(report["removed_orphans"]) == 1 and peer_names(fake) == []


async def test_sync_keeps_a_peer_of_a_draining_exit(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw = await scene(api, worker, world)
    server, _, fake = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    async with session_factory() as session:  # DRAINING без задачи: сверка идёт посреди снятия
        row = await session.get(GatewayExit, UUID(exit_["id"]))
        assert row is not None
        row.state = ExitState.DRAINING
        await session.commit()
    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/sync"))
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_wg")
    assert report["removed_orphans"] == [] and len(peer_names(fake)) == 1


# --- sync и health шлюза ---


async def test_sync_of_the_gateway_reports_exits_updates_health_and_takes_no_action(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    a, b = await exit_server(world, "198.51.100.20"), await exit_server(world, "198.51.100.21")
    first = await add_exit(api, worker, gw.sid, a[0].id)
    second = await add_exit(api, worker, gw.sid, b[0].id)
    handshake = fresh_handshake()
    gw.gateway.exit_handshakes = {1: handshake}
    gw.gateway.exit_interfaces.remove("gwx2")  # второй выход пропал с шлюза
    commands = len(gw.gateway.commands)

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert sync["status"] == "success", sync
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert [(item["slot"], item["interface_up"], item["health"], item["state"]) for item in report["exits"]] == [
        (1, True, "ok", "active"),
        (2, False, "down", "active"),
    ]
    assert [item["exit_id"] for item in report["exits"]] == [first["id"], second["id"]]
    assert report["issues"] == ["Exit gwx2 is down/missing on the gateway"]
    shown = {item["slot"]: item for item in await list_exits(api, gw.sid)}
    assert shown[1]["health"] == "ok" and shown[1]["last_handshake_at"] is not None and shown[1]["checked_at"]
    assert shown[2]["health"] == "down" and shown[2]["last_handshake_at"] is None
    assert not any("awg-quick up" in command or "start.sh" in command for command in gw.gateway.commands[commands:])
    assert gw.gateway.exit_interfaces == ["gwx1"]  # автоматических действий в 24.2 нет


async def test_sync_of_the_gateway_names_an_unknown_tunnel_and_ignores_a_pending_exit(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    assert (await post_exit(api, gw.sid, server.id)).status_code == 202  # PENDING, задачу не выполняем
    gw.gateway.exit_interfaces.append("gwx9")
    gw.gateway.exit_configs["gwx9"] = "[Interface]\n[Peer]\nPublicKey = " + "A" * 43 + "=\n"
    await world.session.rollback()

    # sync ставится без воркера для PENDING-задачи: очередь содержит обе, выполняем обе
    sync_job = (await api.post(f"/api/v1/servers/{gw.sid}/sync")).json()
    await drain(worker)
    result = (await api.get(f"/api/v1/jobs/{sync_job['id']}")).json()["result"]
    report = next(item for item in result["protocols"] if item["type"] == "amnezia_gateway")
    assert any("Unknown exit tunnel gwx9" in issue for issue in report["issues"])


async def test_health_of_the_gateway_follows_the_exit_tunnels_and_refreshes_the_rows(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    await add_exit(api, worker, gw.sid, server.id)

    stale = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/health"))
    gateway = next(item for item in stale["result"]["protocols"] if item["type"] == "amnezia_gateway")
    exits_check = next(check for check in gateway["checks"] if check["name"] == "exits")
    assert not exits_check["ok"] and exits_check["detail"] == "1 exit tunnel(s), 0 healthy: gwx1 no_handshake"
    assert (await list_exits(api, gw.sid))[0]["health"] == "no_handshake"

    gw.gateway.exit_handshakes = {1: fresh_handshake()}
    healthy = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/health"))
    gateway = next(item for item in healthy["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert all(check["ok"] for check in gateway["checks"]), gateway["checks"]
    assert healthy["result"]["status"] == "ok"
    (shown,) = await list_exits(api, gw.sid)
    assert shown["health"] == "ok" and shown["last_handshake_at"] is not None


async def test_health_of_a_stopped_gateway_keeps_the_old_exit_data(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    await add_exit(api, worker, gw.sid, server.id)
    gw.gateway.containers[GATEWAY_LAYOUT.container_name] = "exited"
    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/health"))
    assert job["status"] == "success" and job["result"]["status"] == "degraded"
    assert (await list_exits(api, gw.sid))[0]["health"] is None


async def test_server_busy_error_is_the_only_error_class_that_requeues() -> None:
    """Контракт воркера: ServerBusyError возвращает задачу в очередь, прочие ошибки её завершают (см. JobWorker)."""
    assert issubclass(ServerBusyError, Exception) and ServerBusyError.__module__ == "core.exceptions"
    assert inspect.iscoroutinefunction(JobWorker.process_next)


# --- ревью 24.2 -----------------------------------------------------------------------------------------------------


async def test_sync_removes_only_peers_named_exactly_gw_and_twelve_hex_and_reports_the_rest_as_foreign(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    """`gw-office` из приложения AmneziaVPN - чужой клиент, его sync не трогает."""
    _, _, fake = await exit_server(world, "198.51.100.20")
    server_id = next(sid for sid, f in world.fakes.items() if f is fake)
    async with FakeSSHClient(fake) as ssh:
        provider = worker_module_provider(ssh)
        orphan = await provider.create_client("gw-0123456789ab")
        foreign = [await provider.create_client(name) for name in ("gw-office", "gw-0123456789abc", "gw-0123456789AB")]

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{server_id}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_wg")
    assert [item["public_key"] for item in report["removed_orphans"]] == [orphan.public_key]
    assert {item["public_key"] for item in report["foreign"]} == {client.public_key for client in foreign}
    assert all(client.public_key in fake.config_peer_keys() for client in foreign)


def test_exit_client_names_are_recognised_exactly() -> None:
    from uuid import uuid4

    from modules.gateway.naming import exit_client_name as build
    from modules.gateway.naming import is_exit_client_name

    assert is_exit_client_name(build(uuid4()))
    for name in (
        None,
        "",
        "gw-",
        "gw-office",
        "gw-0123456789a",
        "gw-0123456789abc",
        "gw-0123456789AB",
        "gw-0123456789ab" + chr(10),
    ):
        assert not is_exit_client_name(name)


async def test_a_failed_tunnel_listing_does_not_lose_the_sync_report_of_the_gateway(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.ssh_exception import CommandExecutionError
    from infrastructure.vpn.gateway import AmneziaGatewayProvider

    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    await add_exit(api, worker, gw.sid, server.id)
    await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))
    before = (await list_exits(api, gw.sid))[0]

    async def failing(self: AmneziaGatewayProvider, expected: Any = ()) -> Any:
        raise CommandExecutionError("Failed to list interfaces of the gateway container")

    monkeypatch.setattr(AmneziaGatewayProvider, "exit_status", failing)
    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert sync["status"] == "success", sync
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert report["exits"] == [] and "Failed to list exit tunnels on the gateway" in report["issues"]
    assert report["clients_on_server"] == 0 and report["state"] == "running"  # отчёт по клиентам на месте
    assert (await list_exits(api, gw.sid))[0]["checked_at"] == before["checked_at"]  # health не тронут


async def test_delete_takes_the_gateway_lock_like_post(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST и DELETE одного выхода сериализуются блокировкой строки шлюза: ADD и REMOVE не встанут вместе."""
    gw = await scene(api, worker, world)
    server, _, _ = await exit_server(world, "198.51.100.20")
    exit_ = await add_exit(api, worker, gw.sid, server.id)
    locked: list[UUID] = []
    original = gateway_repository.GatewayExitRepository.lock_gateway

    async def recording(self: Any, gateway_protocol_id: UUID) -> None:
        locked.append(gateway_protocol_id)
        await original(self, gateway_protocol_id)

    monkeypatch.setattr(gateway_repository.GatewayExitRepository, "lock_gateway", recording)
    assert (await api.delete(exits_url(gw.sid, exit_["id"]))).status_code == 202
    assert locked == [gw.gateway_protocol_id]
    assert (await post_exit(api, gw.sid, server.id)).status_code == 409  # и DELETE, и POST видят задачу друг друга
    assert len(await world.jobs(JobType.GATEWAY_ADD_EXIT)) == 1
