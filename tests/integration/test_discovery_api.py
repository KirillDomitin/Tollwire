"""API поиска Amnezia (этап 23.2): POST /servers/{id}/discover (202 + задача) и GET /servers/{id}/discovery (последний
успешный отчёт со статусом каждого клиента относительно БД панели). Воркер настоящий, SSH фейковый."""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.vpn.awg_config import generate_secret_keys
from infrastructure.workers.worker import JobWorker
from modules.audit.actor import SYSTEM_ACTOR
from modules.auth.models.admin import AdminRole
from modules.jobs.models.job import JobType
from modules.jobs.service import JobService
from tests.fakes.fake_ssh import AppPeer
from tests.fixtures import World
from tests.integration.conftest import drain, login_client, make_admin
from tests.integration.discovery_support import (
    CLIENT_KEYS,
    NOW,
    PROTOCOL_KEYS,
    add_app,
    assert_no_secrets,
    run_discovery,
)

MISSING = "00000000-0000-0000-0000-000000000000"


def key() -> str:
    return generate_secret_keys().public_key


async def discovery(api: httpx.AsyncClient, server_id: Any) -> dict[str, Any]:
    response = await api.get(f"/api/v1/servers/{server_id}/discovery")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def statuses(report: dict[str, Any]) -> dict[str, str]:
    (protocol,) = report["protocols"]
    return {client["public_key"]: client["status"] for client in protocol["clients"]}


# --- POST /discover ---


async def test_post_discover_returns_202_with_a_pending_job(api: httpx.AsyncClient, world: World) -> None:
    server, _, _ = await world.server(install=False)
    response = await api.post(f"/api/v1/servers/{server.id}/discover")
    assert response.status_code == 202
    job = response.json()
    assert (job["type"], job["status"], job["server_id"]) == ("discover_vpn", "pending", str(server.id))
    assert job["result"] is None


async def test_posted_discover_job_is_done_by_the_worker_and_visible_in_jobs(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, _, fake = await world.server(install=False)
    add_app(fake, peers=[key()])
    job_id = (await api.post(f"/api/v1/servers/{server.id}/discover")).json()["id"]
    await drain(worker)
    done = (await api.get(f"/api/v1/jobs/{job_id}")).json()
    assert done["status"] == "success" and [p["type"] for p in done["result"]["protocols"]] == ["amnezia_app_awg2"]


async def test_post_discover_unknown_server_is_404(api: httpx.AsyncClient) -> None:
    response = await api.post(f"/api/v1/servers/{MISSING}/discover")
    assert response.status_code == 404 and response.json()["detail"] == "Server not found"  # не «маршрут не найден»


async def test_post_discover_without_ssh_credentials_is_409(api: httpx.AsyncClient) -> None:
    server = (await api.post("/api/v1/servers", json={"name": "bare", "host": "203.0.113.50"})).json()
    response = await api.post(f"/api/v1/servers/{server['id']}/discover")
    assert response.status_code == 409  # как у health: задача без учётки всё равно упала бы


async def test_post_discover_with_garbage_id_is_422(api: httpx.AsyncClient) -> None:
    assert (await api.post("/api/v1/servers/not-a-uuid/discover")).status_code == 422


async def test_discover_endpoints_require_login(client: httpx.AsyncClient, world: World) -> None:
    server, _, _ = await world.server(install=False)
    assert (await client.post(f"/api/v1/servers/{server.id}/discover")).status_code == 401
    assert (await client.get(f"/api/v1/servers/{server.id}/discovery")).status_code == 401


async def test_discover_permissions_follow_health_check_and_server_read(session: AsyncSession, world: World) -> None:
    server, _, _ = await world.server(install=False)
    expected = {
        AdminRole.VIEWER: (403, 200),
        AdminRole.OPERATOR: (202, 200),
        AdminRole.SUPERADMIN: (202, 200),
    }
    for role, (post_status, get_status) in expected.items():
        async with login_client(await make_admin(session, role)) as http:
            assert (await http.post(f"/api/v1/servers/{server.id}/discover")).status_code == post_status, role
            assert (await http.get(f"/api/v1/servers/{server.id}/discovery")).status_code == get_status, role


async def test_viewer_cannot_queue_discovery_job(session: AsyncSession, world: World) -> None:
    server, _, _ = await world.server(install=False)
    async with login_client(await make_admin(session, AdminRole.VIEWER)) as http:
        assert (await http.post(f"/api/v1/servers/{server.id}/discover")).status_code == 403
    assert await world.jobs(JobType("discover_vpn")) == []  # отказ — задача не создана


# --- GET /discovery ---


async def test_get_discovery_before_any_discovery_is_empty(api: httpx.AsyncClient, world: World) -> None:
    server, _, _ = await world.server(install=False)
    assert await discovery(api, server.id) == {"checked_at": None, "protocols": []}


async def test_get_discovery_unknown_server_is_404(api: httpx.AsyncClient) -> None:
    response = await api.get(f"/api/v1/servers/{MISSING}/discovery")
    assert response.status_code == 404 and response.json()["detail"] == "Server not found"  # не «маршрут не найден»


async def test_get_discovery_returns_last_report_in_the_agreed_shape(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, _, fake = await world.server(install=False)
    named, nameless = key(), key()
    app = add_app(
        fake,
        peers=[AppPeer(named, "10.8.1.2/32", handshake=NOW), AppPeer(nameless, "10.8.1.3/32")],
        clients_table={named: "Phone"},
        port=41234,
    )
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    await run_discovery(world, worker, server)
    report = await discovery(api, server.id)

    assert set(report) == {"checked_at", "protocols"}
    checked_at = datetime.fromisoformat(report["checked_at"])
    assert checked_at.tzinfo is not None and before <= checked_at <= datetime.now(timezone.utc) + timedelta(seconds=1)
    (protocol,) = report["protocols"]
    assert set(protocol) == PROTOCOL_KEYS
    assert (protocol["type"], protocol["container"], protocol["state"]) == (
        "amnezia_app_awg2",
        "amnezia-awg2",
        "running",
    )
    assert (protocol["image"], protocol["port"]) == (app.image, 41234)
    assert all(set(client) == CLIENT_KEYS | {"status"} for client in protocol["clients"])
    first, second = protocol["clients"]
    assert (first["public_key"], first["address"], first["name"], first["supported"]) == (
        named,
        "10.8.1.2",
        "Phone",
        True,
    )
    assert datetime.fromisoformat(first["latest_handshake"]) == datetime.fromtimestamp(NOW, tz=timezone.utc)
    assert (second["name"], second["latest_handshake"]) == (None, None)


async def test_get_discovery_does_not_go_to_the_server(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    add_app(fake, peers=[key()])
    await run_discovery(world, worker, server)
    commands = len(fake.commands)
    await discovery(api, server.id)
    await discovery(api, server.id)
    assert len(fake.commands) == commands  # отчёт берётся из последней задачи, а не из свежего опроса


async def test_get_discovery_response_has_no_secrets(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    v1 = add_app(fake, "amnezia-awg", peers=[key()])
    v2 = add_app(fake, "amnezia-awg2", peers=[key(), key()])
    await run_discovery(world, worker, server)
    response = await api.get(f"/api/v1/servers/{server.id}/discovery")
    assert_no_secrets(response.json(), v1, v2)
    for secret in [*v1.secrets, *v2.secrets]:
        assert secret not in response.text


async def test_get_discovery_of_stopped_container_has_state_and_no_clients(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, _, fake = await world.server(install=False)
    add_app(fake, peers=[key()], running=False)
    await run_discovery(world, worker, server)
    (protocol,) = (await discovery(api, server.id))["protocols"]
    assert (protocol["state"], protocol["clients"]) == ("stopped", [])


async def test_get_discovery_of_clean_server_is_empty_but_checked(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, _, _ = await world.server(install=False)
    await run_discovery(world, worker, server)
    report = await discovery(api, server.id)
    assert report["protocols"] == [] and report["checked_at"] is not None  # «искали, не нашли» ≠ «не искали»


async def test_get_discovery_belongs_to_its_own_server(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    first, _, first_fake = await world.server(install=False)
    second, _, _ = await world.server(install=False)
    add_app(first_fake, peers=[key()])
    await run_discovery(world, worker, first)
    assert len((await discovery(api, first.id))["protocols"]) == 1
    assert await discovery(api, second.id) == {"checked_at": None, "protocols": []}


# --- статусы клиентов ---


async def test_client_statuses_new_in_panel_revoked_and_unsupported(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, protocol, fake = await world.server()
    _, _, _, live = await world.issued(protocol)
    _, _, _, revoked = await world.issued(protocol)
    revoked.revoked_at = datetime.now(timezone.utc)
    await world.session.commit()
    brand_new, wide = key(), key()
    add_app(
        fake,
        peers=[
            AppPeer(live.client_public_key, "10.8.1.101/32"),
            AppPeer(revoked.client_public_key, "10.8.1.102/32"),
            AppPeer(brand_new, "10.8.1.103/32"),
            AppPeer(wide, "10.8.1.104/32, 10.8.1.105/32"),
        ],
    )
    await run_discovery(world, worker, server)
    assert statuses(await discovery(api, server.id)) == {
        live.client_public_key: "in_panel",
        revoked.client_public_key: "revoked_in_panel",
        brand_new: "new",
        wide: "unsupported",
    }


async def test_client_status_is_computed_at_request_time(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    """Отчёт хранится в задаче, а статус считается заново по БД: отозвали доступ — статус сменился без нового поиска."""
    server, protocol, fake = await world.server()
    _, _, _, credential = await world.issued(protocol)
    add_app(fake, peers=[credential.client_public_key])
    await run_discovery(world, worker, server)
    assert statuses(await discovery(api, server.id)) == {credential.client_public_key: "in_panel"}

    credential.revoked_at = datetime.now(timezone.utc)
    await world.session.commit()
    assert statuses(await discovery(api, server.id)) == {credential.client_public_key: "revoked_in_panel"}


async def test_key_known_only_on_another_server_is_new(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    _, other_protocol, _ = await world.server()
    _, _, _, elsewhere = await world.issued(other_protocol)  # ключ клиента панели на ДРУГОМ сервере
    add_app(fake, peers=[elsewhere.client_public_key])
    await run_discovery(world, worker, server)
    assert statuses(await discovery(api, server.id)) == {elsewhere.client_public_key: "new"}


async def test_live_credential_wins_over_revoked_one_with_the_same_key(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, protocol, fake = await world.server()
    _, _, _, old = await world.issued(protocol)
    _, _, _, current = await world.issued(protocol)
    old.revoked_at = datetime.now(timezone.utc)
    current.client_public_key = old.client_public_key  # тот же клиент выдан заново
    await world.session.commit()
    add_app(fake, peers=[old.client_public_key])
    await run_discovery(world, worker, server)
    assert statuses(await discovery(api, server.id)) == {old.client_public_key: "in_panel"}


async def test_discovery_does_not_create_anything_in_the_panel(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, protocol, fake = await world.server()
    add_app(fake, peers=[key(), key()])
    await run_discovery(world, worker, server)
    await discovery(api, server.id)
    protocols = (await api.get(f"/api/v1/servers/{server.id}/protocols")).json()
    assert [p["id"] for p in protocols] == [str(protocol.id)]  # только то, что поставила панель


# --- какой отчёт считается «последним» ---


async def test_newer_successful_discovery_replaces_the_older_one(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, _, fake = await world.server(install=False)
    a, b = key(), key()
    app = add_app(fake, peers=[a, b])
    await run_discovery(world, worker, server)
    assert set(statuses(await discovery(api, server.id))) == {a, b}

    app.peers = [peer for peer in app.peers if peer.public_key == a]
    app.files[app.config_path] = app.render_config()
    await run_discovery(world, worker, server)
    assert set(statuses(await discovery(api, server.id))) == {a}


async def test_failed_pending_and_foreign_jobs_do_not_replace_the_last_successful_report(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, _, fake = await world.server(install=False)
    a = key()
    add_app(fake, peers=[a])
    await run_discovery(world, worker, server)

    health = await JobService(world.session, SYSTEM_ACTOR).create(JobType.HEALTH_CHECK, server_id=server.id)
    await drain(worker)  # у отчёта health тоже есть ключ "protocols" — но это не поиск
    await world.session.refresh(health)
    assert health.result is not None and "protocols" in health.result

    fake.reachable = False
    failed = await run_discovery(world, worker, server)
    assert failed.status.value == "failed"
    await JobService(world.session, SYSTEM_ACTOR).create(JobType("discover_vpn"), server_id=server.id)  # ждёт в очереди

    report = await discovery(api, server.id)
    assert report["checked_at"] is not None and statuses(report) == {a: "new"}


async def test_other_servers_job_is_not_used(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    server, _, _ = await world.server(install=False)
    other, _, other_fake = await world.server(install=False)
    add_app(other_fake, peers=[key()])
    await run_discovery(world, worker, other)
    assert await discovery(api, server.id) == {"checked_at": None, "protocols": []}


async def test_discovery_survives_server_with_both_app_containers(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, _, fake = await world.server(install=False)
    add_app(fake, "amnezia-awg", peers=[key()])
    add_app(fake, "amnezia-awg2", peers=[key()])
    await run_discovery(world, worker, server)
    report = await discovery(api, server.id)
    assert [p["type"] for p in report["protocols"]] == ["amnezia_app_awg", "amnezia_app_awg2"]
    assert all(set(p) == PROTOCOL_KEYS for p in report["protocols"])
