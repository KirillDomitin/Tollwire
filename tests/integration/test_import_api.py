"""POST /servers/{id}/import (этап 23.4): постановка задачи IMPORT_CLIENTS. Здесь — только API: права, валидация (422),
доменные отказы (404/409), параметры задачи без секретов. Что делает воркер — в test_import_job.py.

Ключи клиентов берутся из последнего отчёта поиска (GET /discovery), поэтому перед импортом выполняется поиск."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.workers.worker import JobWorker
from modules.auth.models.admin import AdminRole
from modules.jobs.models.job import Job, JobStatus
from modules.vpn_users.models.user import VPNUser
from tests.fakes.fake_ssh import AppPeer
from tests.fixtures import World
from tests.import_support import (
    PROTOCOL_TYPE,
    Scene,
    count,
    import_action,
    import_job_type,
    make_scene,
    new_key,
    set_allowed_ips,
)
from tests.integration.conftest import drain, login_client, make_admin
from tests.integration.discovery_support import add_app, run_discovery

MISSING = "00000000-0000-0000-0000-000000000000"


def future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def client(key: str, username: str = "Phone", **extra: Any) -> dict[str, Any]:
    return {"public_key": key, "username": username, **extra}


def body(items: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    return {"protocol_type": PROTOCOL_TYPE, "valid_until": future(), "clients": items, **overrides}


async def scene_with_report(world: World, worker: JobWorker, **kwargs: Any) -> Scene:
    """Сервер с приложением и сохранённым отчётом поиска — то, что видит админ перед импортом."""
    scene = await make_scene(world, **kwargs)
    await run_discovery(world, worker, scene.server)
    return scene


async def post_import(api: httpx.AsyncClient, server_id: Any, payload: dict[str, Any]) -> httpx.Response:
    return await api.post(f"/api/v1/servers/{server_id}/import", json=payload)


async def import_jobs(world: World) -> list[Any]:
    query = select(Job).where(Job.type == import_job_type()).execution_options(populate_existing=True)
    return list((await world.session.execute(query)).scalars())


# --- постановка ---


async def test_post_import_returns_202_with_a_pending_job(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    response = await post_import(api, scene.server.id, body([client(scene.keys[0])]))
    assert response.status_code == 202, response.text
    job = response.json()
    assert (job["type"], job["status"], job["server_id"]) == ("import_clients", "pending", str(scene.server.id))
    assert job["result"] is None and job["error"] is None


async def test_post_import_does_not_touch_the_server_or_the_panel_data(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    commands = len(scene.fake.commands)
    assert (await post_import(api, scene.server.id, body([client(scene.keys[0])]))).status_code == 202
    assert len(scene.fake.commands) == commands  # проверки по отчёту поиска, на сервер запрос не ходит
    assert await count(world, VPNUser) == 0  # пользователей создаёт воркер, а не API


async def test_posted_job_stores_params_without_secrets_and_the_initiator(
    api: httpx.AsyncClient, world: World, worker: JobWorker, admin: Any
) -> None:
    scene = await scene_with_report(world, worker)
    known = (await api.post("/api/v1/vpn-users", json={"username": "Known", "email": "known@example.com"})).json()
    valid_until = future(10)
    payload = body(
        [
            client(scene.keys[0], "New one", email="new@example.com"),
            client(scene.keys[1], "Existing", user_id=known["id"]),
            client(scene.keys[2], "Bare"),
        ],
        valid_until=valid_until,
    )
    response = await post_import(api, scene.server.id, payload)
    assert response.status_code == 202, response.text

    (job,) = await import_jobs(world)
    assert job.admin_id == admin.admin.id  # аудит воркера пойдёт от имени инициатора
    params = job.params
    assert set(params) == {"protocol_type", "valid_until", "clients"}
    assert params["protocol_type"] == PROTOCOL_TYPE
    assert datetime.fromisoformat(params["valid_until"]) == datetime.fromisoformat(valid_until)
    assert params["clients"] == [
        {"public_key": scene.keys[0], "username": "New one", "email": "new@example.com", "user_id": None},
        {"public_key": scene.keys[1], "username": "Existing", "email": None, "user_id": known["id"]},
        {"public_key": scene.keys[2], "username": "Bare", "email": None, "user_id": None},
    ]
    assert not any(secret in str(params) for secret in scene.secrets)  # PSK и ключ сервера в параметры не попадают


async def test_post_import_writes_started_audit_with_the_initiator(
    api: httpx.AsyncClient, world: World, worker: JobWorker, admin: Any
) -> None:
    scene = await scene_with_report(world, worker)
    assert (await post_import(api, scene.server.id, body([client(scene.keys[0], "Secret Name")]))).status_code == 202
    (started,) = await world.audit(import_action("started"))
    assert started.admin_id == admin.admin.id and started.entity_id == scene.server.id
    assert "Secret Name" not in str(started.meta) and scene.keys[0] not in str(started.meta)


async def test_posted_job_is_executed_by_the_worker_and_visible_in_jobs(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    job_id = (await post_import(api, scene.server.id, body([client(scene.keys[0])]))).json()["id"]
    assert await drain(worker) == 1
    done = (await api.get(f"/api/v1/jobs/{job_id}")).json()
    assert done["status"] == "success" and len(done["result"]["imported"]) == 1


async def test_user_id_of_an_existing_user_is_accepted(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    scene = await scene_with_report(world, worker)
    known = (await api.post("/api/v1/vpn-users", json={"username": "Known", "email": "known@example.com"})).json()
    response = await post_import(api, scene.server.id, body([client(scene.keys[0], "ignored", user_id=known["id"])]))
    assert response.status_code == 202, response.text


async def test_valid_until_just_in_the_future_is_accepted(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert (await post_import(api, scene.server.id, body([client(scene.keys[0])], valid_until=soon))).status_code == 202


async def test_import_is_allowed_again_after_the_previous_import_finished(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    payload = body([client(scene.keys[0])])
    assert (await post_import(api, scene.server.id, payload)).status_code == 202
    await drain(worker)
    assert (await post_import(api, scene.server.id, payload)).status_code == 202  # повтор безопасен: ответит «already»


# --- 422: невалидный ввод ---


def invalid_payloads() -> dict[str, dict[str, Any]]:
    good = new_key()
    return {
        "bad-public-key": body([client("not-a-key")]),
        "public-key-with-newline": body([client(good + "\n")]),
        "duplicate-keys": body([client(good, "a"), client(good, "b")]),
        "no-clients": body([]),
        "too-many-clients": body([client(new_key(), f"u{i}") for i in range(251)]),
        "wg-protocol-type": body([client(good)], protocol_type="amnezia_wg"),
        "unknown-protocol-type": body([client(good)], protocol_type="openvpn"),
        "no-protocol-type": {"valid_until": future(), "clients": [client(good)]},
        "no-valid-until": {"protocol_type": PROTOCOL_TYPE, "clients": [client(good)]},
        "naive-valid-until": body([client(good)], valid_until=(datetime.now() + timedelta(days=3)).isoformat()),
        "valid-until-in-the-past": body([client(good)], valid_until=future(-1)),
        "valid-until-is-garbage": body([client(good)], valid_until="tomorrow"),
        "empty-username": body([client(good, "")]),
        "username-with-nul": body([client(good, "ab\x00c")]),
        "username-with-newline": body([client(good, "ab\ncd")]),
        "username-over-100": body([client(good, "x" * 101)]),
        "no-username": body([{"public_key": good}]),
        "bad-email": body([client(good, email="not-an-email")]),
        "bad-user-id": body([client(good, user_id="not-a-uuid")]),
        "user-id-and-email": body([client(good, email="a@example.com", user_id=str(uuid4()))]),
        "clients-is-not-a-list": body([client(good)], clients=client(good)),
    }


@pytest.mark.parametrize("name", sorted(invalid_payloads()))
async def test_invalid_import_body_is_422_and_queues_nothing(
    name: str, api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    payload = invalid_payloads()[name]
    response = await post_import(api, scene.server.id, payload)
    assert response.status_code == 422, (name, response.text)
    assert await import_jobs(world) == []


async def test_extra_client_fields_never_reach_the_job_params(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    """Секрет в теле (например, PSK) в параметры задачи попасть не должен, как бы запрос ни закончился."""
    scene = await scene_with_report(world, worker)
    psk = "p" * 43 + "="
    response = await post_import(api, scene.server.id, body([{**client(scene.keys[0]), "psk": psk}]))
    assert response.status_code in (202, 422) and psk not in response.text
    for job in await import_jobs(world):
        assert psk not in str(job.params) and set(job.params["clients"][0]) == {
            "public_key",
            "username",
            "email",
            "user_id",
        }


async def test_username_of_exactly_100_characters_is_accepted(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    assert (await post_import(api, scene.server.id, body([client(scene.keys[0], "x" * 100)]))).status_code == 202


async def test_exactly_250_clients_are_accepted_by_the_schema(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await make_scene(world, count=250)
    await run_discovery(world, worker, scene.server)
    response = await post_import(api, scene.server.id, body([client(k, f"u{i}") for i, k in enumerate(scene.keys)]))
    assert response.status_code == 202, response.text


async def test_unknown_server_is_404_with_a_clear_detail(api: httpx.AsyncClient) -> None:
    response = await post_import(api, MISSING, body([client(new_key())]))
    assert response.status_code == 404 and response.json()["detail"] == "Server not found"


async def test_garbage_server_id_is_422(api: httpx.AsyncClient) -> None:
    assert (await post_import(api, "not-a-uuid", body([client(new_key())]))).status_code == 422


async def test_unknown_user_id_is_404_and_queues_nothing(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    response = await post_import(api, scene.server.id, body([client(scene.keys[0], user_id=str(uuid4()))]))
    assert response.status_code == 404
    assert await import_jobs(world) == []


async def test_server_without_ssh_credentials_is_409(api: httpx.AsyncClient) -> None:
    server = (await api.post("/api/v1/servers", json={"name": "bare", "host": "203.0.113.50"})).json()
    response = await post_import(api, server["id"], body([client(new_key())]))
    assert response.status_code == 409  # как у health и discover: без учётки задача всё равно упала бы


# --- 409: отчёт поиска ---


async def test_key_absent_from_the_last_discovery_report_is_409(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    response = await post_import(api, scene.server.id, body([client(scene.keys[0]), client(new_key(), "stranger")]))
    assert response.status_code == 409, response.text
    assert await import_jobs(world) == []  # одна чужая запись — отказ целиком


async def test_import_without_any_discovery_report_is_409(api: httpx.AsyncClient, world: World) -> None:
    scene = await make_scene(world)  # поиск не запускали
    response = await post_import(api, scene.server.id, body([client(scene.keys[0])]))
    assert response.status_code == 409
    assert await import_jobs(world) == []


async def test_key_added_to_the_server_after_the_report_is_409_until_the_next_discovery(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=1)
    latecomer = new_key()
    scene.app.peers.append(AppPeer(latecomer, "10.8.1.77/32"))
    scene.app.files[scene.app.config_path] = scene.app.render_config()
    assert (await post_import(api, scene.server.id, body([client(latecomer)]))).status_code == 409
    await run_discovery(world, worker, scene.server)
    assert (await post_import(api, scene.server.id, body([client(latecomer)]))).status_code == 202


async def test_unsupported_peer_is_409(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    scene = await make_scene(world, count=2)
    set_allowed_ips(scene.app, scene.keys[0], "10.8.1.2/32, 10.8.1.99/32")  # несколько адресов — панель не умеет
    await run_discovery(world, worker, scene.server)
    response = await post_import(api, scene.server.id, body([client(scene.keys[0])]))
    assert response.status_code == 409
    assert (await post_import(api, scene.server.id, body([client(scene.keys[1])]))).status_code == 202
    assert len(await import_jobs(world)) == 1


async def test_key_from_the_other_app_container_is_409(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    server, _, fake = await world.server(install=False)
    v1_key, v2_key = new_key(), new_key()
    add_app(fake, "amnezia-awg", peers=[v1_key])
    add_app(fake, "amnezia-awg2", peers=[v2_key])
    await run_discovery(world, worker, server)
    assert (await post_import(api, server.id, body([client(v1_key)]))).status_code == 409  # v1-ключ не из awg2
    assert (
        await post_import(api, server.id, body([client(v2_key)], protocol_type="amnezia_app_awg"))
    ).status_code == 409
    assert await import_jobs(world) == []
    assert (
        await post_import(api, server.id, body([client(v1_key)], protocol_type="amnezia_app_awg"))
    ).status_code == 202


async def test_second_import_while_one_is_unfinished_is_409(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    assert (await post_import(api, scene.server.id, body([client(scene.keys[0])]))).status_code == 202
    second = await post_import(api, scene.server.id, body([client(scene.keys[1])]))
    assert second.status_code == 409, second.text
    assert len(await import_jobs(world)) == 1


async def test_unfinished_import_of_another_server_does_not_block(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    first = await scene_with_report(world, worker)
    second = await scene_with_report(world, worker)
    assert (await post_import(api, first.server.id, body([client(first.keys[0])]))).status_code == 202
    assert (await post_import(api, second.server.id, body([client(second.keys[0])]))).status_code == 202


async def test_failed_import_does_not_block_the_next_one(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    scene.fake.reachable = False
    assert (await post_import(api, scene.server.id, body([client(scene.keys[0])]))).status_code == 202
    await drain(worker)
    (job,) = await import_jobs(world)
    assert job.status == JobStatus.FAILED
    scene.fake.reachable = True
    assert (await post_import(api, scene.server.id, body([client(scene.keys[0])]))).status_code == 202


# --- права ---


async def test_import_permissions_follow_servers_manage(session: AsyncSession, world: World, worker: JobWorker) -> None:
    scene = await scene_with_report(world, worker)
    payload = body([client(scene.keys[0])])
    for role, expected in ((AdminRole.VIEWER, 403), (AdminRole.OPERATOR, 403), (AdminRole.SUPERADMIN, 202)):
        async with login_client(await make_admin(session, role)) as http:
            response = await post_import(http, scene.server.id, payload)
            assert response.status_code == expected, (role, response.text)
    assert len(await import_jobs(world)) == 1  # отказы задач не создали


async def test_import_requires_login(client: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    scene = await scene_with_report(world, worker)
    response = await post_import(client, scene.server.id, body([{"public_key": scene.keys[0], "username": "x"}]))
    assert response.status_code == 401
    assert await import_jobs(world) == []
