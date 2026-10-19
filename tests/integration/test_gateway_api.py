"""Шлюз (этап 24.1) через HTTP и воркер: `POST/DELETE /servers/{id}/protocol` с необязательным телом, тип в
Job.params, установка/снятие шлюза, выдача клиента на протокол шлюза, sync/health. SSH - фейковый."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from infrastructure.vpn.awg_config import interface_values, list_peers, remove_peer
from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT
from infrastructure.workers.worker import JobWorker
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import JobStatus, JobType
from modules.jobs.service import JobService
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from tests.fakes.fake_ssh import FakeServer
from tests.fixtures import World
from tests.import_support import add_app_protocol, imported_credential, make_scene
from tests.integration.conftest import drain
from tests.integration.test_mvp_flow import run_job
from tests.unit.test_amnezia_link import decode as decode_share_link

GATEWAY = {"type": "amnezia_gateway"}
NAME = "vpn-admin-gw"
START_SCRIPT = f"{GATEWAY_LAYOUT.host_config_dir}/start.sh"


async def delete_protocol(
    api: httpx.AsyncClient, sid: UUID | str, body: dict[str, Any] | None = None
) -> httpx.Response:
    """DELETE с необязательным телом (у httpx.delete параметра json нет)."""
    return await api.request("DELETE", f"/api/v1/servers/{sid}/protocol", **({} if body is None else {"json": body}))


async def gateway_server(world: World) -> tuple[UUID, FakeServer]:
    """Сервер с SSH-учёткой и фейком, на котором раскладка контейнера - шлюзовая."""
    server, _, _ = await world.server(install=False)
    fake = world.fakes[server.id] = FakeServer(host=server.host, layout=GATEWAY_LAYOUT)
    return server.id, fake


async def install_gateway(api: httpx.AsyncClient, worker: JobWorker, sid: UUID) -> dict[str, Any]:
    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/protocol", json=GATEWAY))
    assert job["status"] == "success", job
    return job


async def protocols(api: httpx.AsyncClient, sid: UUID) -> list[dict[str, Any]]:
    return list((await api.get(f"/api/v1/servers/{sid}/protocols")).json())


# --- тело запроса и Job.params ---------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [None, {}, {"type": "amnezia_wg"}])
async def test_without_body_or_with_wg_the_job_is_the_old_one(
    api: httpx.AsyncClient, world: World, body: dict[str, Any] | None
) -> None:
    """Контракт не изменился: тело необязательно, а обычный протокол не оставляет params (как до шлюза)."""
    server, _, _ = await world.server(install=False)
    for response in (
        await api.request("POST", f"/api/v1/servers/{server.id}/protocol", **({} if body is None else {"json": body})),
        await delete_protocol(api, server.id, body),
    ):
        assert response.status_code == 202, response.text
        assert "params" not in response.json()
    install, remove = await world.jobs(JobType.INSTALL_PROTOCOL), await world.jobs(JobType.REMOVE_PROTOCOL)
    assert [job.params for job in (*install, *remove)] == [None, None]


async def test_gateway_type_goes_into_job_params_and_is_not_exposed(api: httpx.AsyncClient, world: World) -> None:
    server, _, _ = await world.server(install=False)
    install = await api.post(f"/api/v1/servers/{server.id}/protocol", json=GATEWAY)
    remove = await delete_protocol(api, server.id, GATEWAY)
    assert install.status_code == remove.status_code == 202
    assert "params" not in install.json() and "amnezia_gateway" not in install.text
    (install_job,), (remove_job,) = (
        await world.jobs(JobType.INSTALL_PROTOCOL),
        await world.jobs(JobType.REMOVE_PROTOCOL),
    )
    assert install_job.params == remove_job.params == {"protocol_type": "amnezia_gateway"}


@pytest.mark.parametrize(
    "body", [{"type": "amnezia_app_awg"}, {"type": "amnezia_app_awg2"}, {"type": "bogus"}, {"type": None}]
)
@pytest.mark.parametrize("method", ["post", "delete"])
async def test_only_protocols_installed_by_the_panel_are_accepted(
    api: httpx.AsyncClient, world: World, method: str, body: dict[str, Any]
) -> None:
    server, _, _ = await world.server(install=False)
    response = await api.request(method.upper(), f"/api/v1/servers/{server.id}/protocol", json=body)
    assert response.status_code == 422
    assert await world.jobs(JobType.INSTALL_PROTOCOL) == [] and await world.jobs(JobType.REMOVE_PROTOCOL) == []


async def test_no_credentials_and_missing_server_keep_their_codes_with_a_body(
    api: httpx.AsyncClient, world: World
) -> None:
    created = await api.post("/api/v1/servers", json={"name": "bare", "host": "203.0.113.50"})
    assert (await api.post(f"/api/v1/servers/{created.json()['id']}/protocol", json=GATEWAY)).status_code == 409
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await api.post(f"/api/v1/servers/{missing}/protocol", json=GATEWAY)).status_code == 404


# --- установка и снятие шлюза воркером -------------------------------------------------------------------------


async def test_gateway_install_job_creates_container_and_protocol_row(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    job = await install_gateway(api, worker, sid)

    (protocol,) = await protocols(api, sid)
    assert protocol["type"] == "amnezia_gateway" and protocol["port"]
    assert protocol["port"] == int(interface_values(fake.files[GATEWAY_LAYOUT.host_config_file])["ListenPort"])
    assert fake.containers == {NAME: "running"} and fake.ip_rules
    assert job["admin_id"] is not None and job.get("params") is None
    assert await world.audit(AuditAction.PROTOCOL_INSTALL_STARTED, sid)
    assert await world.audit(AuditAction.PROTOCOL_INSTALL_COMPLETED, sid)


async def test_repeated_gateway_install_keeps_one_row_and_the_same_port(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)
    (first,) = await protocols(api, sid)
    config = fake.files[GATEWAY_LAYOUT.host_config_file]

    await install_gateway(api, worker, sid)
    (second,) = await protocols(api, sid)

    assert (second["id"], second["port"]) == (first["id"], first["port"])
    assert fake.files[GATEWAY_LAYOUT.host_config_file] == config  # ключи сервера и порт не перегенерированы


async def test_gateway_remove_job_removes_container_row_and_files(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)

    job = await run_job(api, worker, await delete_protocol(api, sid, GATEWAY))

    assert job["status"] == "success", job
    assert await protocols(api, sid) == []
    assert fake.containers == {} and fake.files == {} and fake.ip_rules == []
    assert await world.audit(AuditAction.PROTOCOL_REMOVE_COMPLETED, sid)


async def test_remove_without_body_does_not_touch_the_gateway(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)

    job = await run_job(api, worker, await delete_protocol(api, sid))  # обычный протокол

    assert job["status"] == "success"
    assert [item["type"] for item in await protocols(api, sid)] == ["amnezia_gateway"]
    assert fake.containers == {NAME: "running"} and START_SCRIPT in fake.files


async def test_install_without_body_still_installs_the_regular_protocol(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    server, _, fake = await world.server(install=False)
    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/protocol"))
    assert job["status"] == "success", job
    assert [item["type"] for item in await protocols(api, server.id)] == ["amnezia_wg"]
    assert set(fake.containers) == {"amnezia-awg"}


async def test_foreign_container_named_like_the_gateway_fails_the_job_and_stays_untouched(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    fake.add_foreign_container("running")

    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/protocol", json=GATEWAY))

    assert job["status"] == "failed" and NAME in job["error"]
    assert await protocols(api, sid) == []
    assert fake.mutating_commands() == [] and fake.writes == [] and fake.containers == {NAME: "running"}


@pytest.mark.parametrize("params", [{"protocol_type": "bogus"}, {"protocol_type": "amnezia_app_awg"}])
@pytest.mark.parametrize("job_type", [JobType.INSTALL_PROTOCOL, JobType.REMOVE_PROTOCOL])
async def test_worker_rejects_unknown_or_unmanaged_protocol_types_in_params(
    world: World, worker: JobWorker, job_type: JobType, params: dict[str, str]
) -> None:
    sid, fake = await gateway_server(world)
    job = await JobService(world.session, SYSTEM_ACTOR).create(job_type, server_id=sid, params=params)

    await drain(worker)

    await world.session.refresh(job)
    assert job.status == JobStatus.FAILED and "protocol" in (job.error or "").lower()
    assert fake.commands == [] and fake.writes == []


async def test_job_without_params_means_the_regular_protocol(world: World, worker: JobWorker) -> None:
    """Задачи, поставленные до шлюза (в очереди или в БД), не имеют params - это обычный протокол."""
    server, _, fake = await world.server(install=False)
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.INSTALL_PROTOCOL, server_id=server.id)
    await drain(worker)
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS and job.params is None and "amnezia-awg" in fake.containers


# --- строки protocols ------------------------------------------------------------------------------------------


async def test_protocol_rows_are_unique_per_server_and_type_but_types_coexist(world: World) -> None:
    server, wg, _ = await world.server()
    assert wg is not None
    repo = ProtocolRepository(world.session)
    await repo.create(VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_GATEWAY, port=40000))
    await repo.create(VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_GATEWAY, port=40001))  # get-or-update
    await world.session.commit()
    rows = await repo.list_by_server_id(server.id)
    assert sorted(row.type.value for row in rows) == ["amnezia_gateway", "amnezia_wg"]
    assert next(row.port for row in rows if row.type == ProtocolType.AMNEZIA_GATEWAY) == 40001

    world.session.add(VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_GATEWAY, port=1))
    with pytest.raises(IntegrityError):
        await world.session.commit()
    await world.session.rollback()


# --- клиент на протоколе шлюза ---------------------------------------------------------------------------------


async def issue_gateway_client(
    api: httpx.AsyncClient, worker: JobWorker, sid: UUID, protocol_id: str, username: str = "alice"
) -> tuple[dict[str, Any], dict[str, Any]]:
    now = datetime.now(timezone.utc)
    user = (await api.post("/api/v1/vpn-users", json={"username": username, "email": f"{username}@example.com"})).json()
    period = {
        "valid_from": (now - timedelta(minutes=1)).isoformat(),
        "valid_until": (now + timedelta(days=30)).isoformat(),
    }
    subscription = (await api.post(f"/api/v1/vpn-users/{user['id']}/subscriptions", json=period)).json()
    grant = (
        await api.post(f"/api/v1/subscriptions/{subscription['id']}/grants", json={"protocol_id": protocol_id})
    ).json()
    assert grant["server_id"] == str(sid)
    job = await run_job(api, worker, await api.post(f"/api/v1/access/{grant['id']}/credentials"))
    assert job["status"] == "success", job
    (credential,) = (await api.get(f"/api/v1/access/{grant['id']}/credentials")).json()
    return grant, credential


async def test_gateway_client_gets_an_address_from_the_gateway_subnet_and_a_matching_link(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)
    (protocol,) = await protocols(api, sid)

    _, credential = await issue_gateway_client(api, worker, sid, protocol["id"])

    assert [peer.address for peer in list_peers(fake.files[GATEWAY_LAYOUT.host_config_file])] == ["10.77.0.2"]
    reveal = await api.post(f"/api/v1/credentials/{credential['id']}/config")
    assert reveal.status_code == 200
    config = reveal.json()["config"]
    assert "Address = 10.77.0.2/32" in config and "DNS = 1.1.1.1, 1.0.0.1" in config
    assert f"Endpoint = 203.0.113.10:{protocol['port']}" in config
    document = decode_share_link(reveal.json()["share_link"])
    assert document["containers"][0]["awg"]["subnet_address"] == "10.77.0.0"
    assert (document["dns1"], document["dns2"]) == ("1.1.1.1", "1.0.0.1")
    assert len(fake.config_peer_keys()) == 1


async def test_gateway_client_is_revoked_on_the_gateway(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)
    (protocol,) = await protocols(api, sid)
    grant, _ = await issue_gateway_client(api, worker, sid, protocol["id"])
    assert len(fake.config_peer_keys()) == 1

    assert (await api.post(f"/api/v1/access/{grant['id']}/revoke")).status_code == 200
    await drain(worker)

    assert fake.config_peer_keys() == set()


# --- снятие протокола при живых клиентах -----------------------------------------------------------------------


async def test_live_gateway_clients_block_removing_the_gateway_but_not_the_regular_protocol(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, _ = await gateway_server(world)
    await install_gateway(api, worker, sid)
    (protocol,) = await protocols(api, sid)
    await issue_gateway_client(api, worker, sid, protocol["id"])

    blocked = await delete_protocol(api, sid, GATEWAY)
    assert blocked.status_code == 409 and "revoke" in blocked.json()["detail"].lower()
    assert (await delete_protocol(api, sid)).status_code == 202  # клиентов у обычного протокола нет


async def test_live_regular_clients_block_removing_the_regular_protocol_but_not_the_gateway(
    api: httpx.AsyncClient, world: World
) -> None:
    server, wg, _ = await world.server()
    assert wg is not None
    await world.issued(wg)
    world.session.add(VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_GATEWAY, port=40000))
    await world.session.commit()

    assert (await delete_protocol(api, server.id)).status_code == 409  # как и раньше
    assert (await delete_protocol(api, server.id, GATEWAY)).status_code == 202


async def test_live_app_clients_still_block_the_regular_protocol_removal(api: httpx.AsyncClient, world: World) -> None:
    """Контракт 23.4: импортированные клиенты приложения блокируют DELETE /protocol (без тела) по-прежнему."""
    scene = await make_scene(world, count=1)
    protocol = await add_app_protocol(world, scene)
    await imported_credential(world, protocol, scene.keys[0], psk=scene.app.peers[0].psk)
    assert (await delete_protocol(api, scene.server.id)).status_code == 409
    assert (await delete_protocol(api, scene.server.id, GATEWAY)).status_code == 202


# --- sync / health ---------------------------------------------------------------------------------------------


async def test_sync_and_health_know_the_gateway(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    sid, _ = await gateway_server(world)
    await install_gateway(api, worker, sid)
    (protocol,) = await protocols(api, sid)
    await issue_gateway_client(api, worker, sid, protocol["id"])

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))
    assert sync["status"] == "success", sync
    report = {item["type"]: item for item in sync["result"]["protocols"]}
    assert report["amnezia_gateway"]["in_db"] is True and report["amnezia_gateway"]["clients_on_server"] == 1
    assert report["amnezia_gateway"]["issues"] == []

    health = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/health"))
    assert health["status"] == "success", health
    gateway = next(item for item in health["result"]["protocols"] if item["type"] == "amnezia_gateway")
    failed = [check["name"] for check in gateway["checks"] if not check["ok"]]
    assert failed == ["exits"]  # выходов в 24.1 нет: сервер DEGRADED, всё остальное в порядке
    assert health["result"]["status"] == "degraded"
    assert gateway["clients"]["total"] == 1


async def test_sync_restores_a_gateway_client_missing_from_the_server(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)
    (protocol,) = await protocols(api, sid)
    await issue_gateway_client(api, worker, sid, protocol["id"])
    (key,) = fake.config_peer_keys()
    config_path = GATEWAY_LAYOUT.host_config_file
    fake.files[config_path], _ = remove_peer(fake.files[config_path], key)
    fake.interface_peers = fake.config_peer_keys()

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert len(report["restored"]) == 1
    assert fake.config_peer_keys() == {key}


async def test_servers_without_a_gateway_get_no_gateway_entries_in_sync_and_health(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    server, _, fake = await world.server()
    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/sync"))
    health = await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/health"))
    assert [item["type"] for item in sync["result"]["protocols"]] == ["amnezia_wg"]
    assert [item["type"] for item in health["result"]["protocols"]] == ["amnezia_wg"]
    assert health["result"]["status"] == "ok"
    assert fake.writes == [] or all("/gw/" not in path for path in fake.writes)
    assert not any(NAME in command and "docker exec" in command for command in fake.commands)


async def test_discovery_ignores_the_gateway(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    sid, _ = await gateway_server(world)
    await install_gateway(api, worker, sid)
    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/discover"))
    assert job["status"] == "success" and job["result"] == {"protocols": []}


# --- тип протокола в query (?type=...) -------------------------------------------------------------------------


async def test_type_can_come_from_the_query_for_post_and_delete(api: httpx.AsyncClient, world: World) -> None:
    """У DELETE тело нередко отбрасывают клиент или прокси - тип можно передать в адресе."""
    server, _, _ = await world.server(install=False)
    url = f"/api/v1/servers/{server.id}/protocol"
    assert (await api.post(url, params={"type": "amnezia_gateway"})).status_code == 202
    assert (await api.request("DELETE", url, params={"type": "amnezia_gateway"})).status_code == 202
    assert (await api.post(url, params={"type": "amnezia_wg"})).status_code == 202
    (gateway_install, wg_install) = sorted(await world.jobs(JobType.INSTALL_PROTOCOL), key=lambda j: j.params is None)
    (remove,) = await world.jobs(JobType.REMOVE_PROTOCOL)
    assert gateway_install.params == remove.params == {"protocol_type": "amnezia_gateway"}
    assert wg_install.params is None


async def test_query_and_body_may_agree_or_the_body_may_leave_the_type_out(
    api: httpx.AsyncClient, world: World
) -> None:
    server, _, _ = await world.server(install=False)
    url = f"/api/v1/servers/{server.id}/protocol"
    for body in (GATEWAY, {}):  # пустое тело не задаёт тип - решает query
        assert (await api.post(url, params={"type": "amnezia_gateway"}, json=body)).status_code == 202
    assert [job.params for job in await world.jobs(JobType.INSTALL_PROTOCOL)] == [
        {"protocol_type": "amnezia_gateway"}
    ] * 2


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_query_and_body_that_differ_are_422_and_create_no_job(
    api: httpx.AsyncClient, world: World, method: str
) -> None:
    server, _, _ = await world.server(install=False)
    url = f"/api/v1/servers/{server.id}/protocol"
    for query, body in (("amnezia_wg", GATEWAY), ("amnezia_gateway", {"type": "amnezia_wg"})):
        response = await api.request(method, url, params={"type": query}, json=body)
        assert response.status_code == 422, response.text
    assert await world.jobs(JobType.INSTALL_PROTOCOL) == [] and await world.jobs(JobType.REMOVE_PROTOCOL) == []


@pytest.mark.parametrize("value", ["amnezia_app_awg", "amnezia_app_awg2", "bogus", ""])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_bad_type_in_the_query_is_422(api: httpx.AsyncClient, world: World, method: str, value: str) -> None:
    server, _, _ = await world.server(install=False)
    response = await api.request(method, f"/api/v1/servers/{server.id}/protocol", params={"type": value})
    assert response.status_code == 422
    assert await world.jobs(JobType.INSTALL_PROTOCOL) == [] and await world.jobs(JobType.REMOVE_PROTOCOL) == []


async def test_query_type_gateway_blocks_on_live_gateway_clients_like_the_body(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, _ = await gateway_server(world)
    await install_gateway(api, worker, sid)
    (protocol,) = await protocols(api, sid)
    await issue_gateway_client(api, worker, sid, protocol["id"])
    assert (
        await api.request("DELETE", f"/api/v1/servers/{sid}/protocol", params={"type": "amnezia_gateway"})
    ).status_code == 409


# --- аудит: protocol_type ----------------------------------------------------------------------------------------


async def test_audit_of_protocol_jobs_carries_the_protocol_type(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, _ = await gateway_server(world)
    await install_gateway(api, worker, sid)
    await run_job(api, worker, await delete_protocol(api, sid, GATEWAY))
    for action in (
        AuditAction.PROTOCOL_INSTALL_STARTED,
        AuditAction.PROTOCOL_INSTALL_COMPLETED,
        AuditAction.PROTOCOL_REMOVE_STARTED,
        AuditAction.PROTOCOL_REMOVE_COMPLETED,
    ):
        (record,) = await world.audit(action, sid)
        assert record.meta is not None and record.meta["protocol_type"] == "amnezia_gateway", action
        assert set(record.meta) <= {"job_id", "protocol_type"}  # ничего лишнего (секретов нет)


async def test_audit_of_a_regular_protocol_job_says_amnezia_wg(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    server, _, _ = await world.server(install=False)
    await run_job(api, worker, await api.post(f"/api/v1/servers/{server.id}/protocol"))
    for action in (AuditAction.PROTOCOL_INSTALL_STARTED, AuditAction.PROTOCOL_INSTALL_COMPLETED):
        (record,) = await world.audit(action, server.id)
        assert record.meta is not None and record.meta["protocol_type"] == "amnezia_wg"


async def test_audit_of_a_failed_gateway_install_has_type_and_error(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway_server(world)
    fake.add_foreign_container("running")
    await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/protocol", json=GATEWAY))
    (record,) = await world.audit(AuditAction.PROTOCOL_INSTALL_FAILED, sid)
    assert record.meta is not None
    assert record.meta["protocol_type"] == "amnezia_gateway" and NAME in record.meta["error"]


async def test_audit_ignores_a_garbage_type_in_params_instead_of_writing_it(world: World, worker: JobWorker) -> None:
    sid, _ = await gateway_server(world)
    await JobService(world.session, SYSTEM_ACTOR).create(
        JobType.INSTALL_PROTOCOL, server_id=sid, params={"protocol_type": "<script>"}
    )
    await drain(worker)
    for action in (AuditAction.PROTOCOL_INSTALL_STARTED, AuditAction.PROTOCOL_INSTALL_FAILED):
        (record,) = await world.audit(action, sid)
        assert record.meta is not None and "protocol_type" not in record.meta
