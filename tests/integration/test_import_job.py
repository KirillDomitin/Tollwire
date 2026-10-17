"""Задача IMPORT_CLIENTS (этап 23.4): воркер настоящий, SSH фейковый. Импорт переносит клиентов установленной AmneziaVPN
в БД панели (пользователь, подписка, грант, credential без конфига, но с публичным ключом, адресом и PSK) и ничего не
меняет на сервере. Основа — повторное чтение сервера, а не отчёт поиска.

Новые имена (JobType.IMPORT_CLIENTS, CredentialSource, AuditAction.CLIENTS_IMPORT_*) берутся в import_support: пока
кода нет, красными становятся тесты, а не вся коллекция."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from core.security import decrypt_secret
from infrastructure.workers.locks import ServerLock
from infrastructure.workers.worker import JobWorker
from modules.access.models import AccessGrantStatus
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.schemas import SubscriptionCreate
from modules.access.service import SubscriptionService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import Job, JobStatus
from modules.jobs.queue import JobQueue
from modules.jobs.service import JobService
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.vpn_users.models.user import VPNUser, VPNUserStatus
from tests.fixtures import World
from tests.import_support import (
    PROTOCOL_TYPE,
    Scene,
    count,
    credential_source,
    credentials_of,
    drop_peer,
    import_action,
    import_job_type,
    make_scene,
    new_key,
    set_allowed_ips,
    strip_psk,
)
from tests.integration.conftest import AdminLogin, drain
from tests.integration.discovery_support import run_discovery

RESULT_KEYS = {"protocol_id", "imported", "already", "gone", "unsupported", "conflict"}


def client(key: str, username: str = "Phone", **extra: Any) -> dict[str, Any]:
    return {"public_key": key, "username": username, **extra}


def future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


async def scene_with_report(world: World, worker: JobWorker, **kwargs: Any) -> Scene:
    scene = await make_scene(world, **kwargs)
    await run_discovery(world, worker, scene.server)
    return scene


async def run_import(
    api: httpx.AsyncClient,
    world: World,
    worker: JobWorker,
    scene: Scene,
    clients: list[dict[str, Any]],
    *,
    valid_until: str | None = None,
    protocol_type: str = PROTOCOL_TYPE,
) -> Job:
    """POST /import -> воркер -> готовая задача."""
    payload = {"protocol_type": protocol_type, "valid_until": valid_until or future(), "clients": clients}
    response = await api.post(f"/api/v1/servers/{scene.server.id}/import", json=payload)
    assert response.status_code == 202, response.text
    await drain(worker)
    job = await world.session.get(Job, UUID(response.json()["id"]))
    assert job is not None
    await world.session.refresh(job)
    return job


def result_of(job: Job) -> dict[str, Any]:
    assert job.status == JobStatus.SUCCESS, job.error
    assert job.result is not None
    return job.result


async def users(world: World) -> list[VPNUser]:
    query = select(VPNUser).order_by(VPNUser.created_at).execution_options(populate_existing=True)
    return list((await world.session.execute(query)).scalars())


async def all_of(world: World, model: Any) -> list[Any]:
    return list((await world.session.execute(select(model).execution_options(populate_existing=True))).scalars())


async def app_protocol(world: World, scene: Scene) -> VPNProtocol | None:
    return await ProtocolRepository(world.session).get_by_server_and_type(
        scene.server.id, ProtocolType.AMNEZIA_APP_AWG2
    )


async def panel_rows(world: World) -> tuple[int, int, int, int]:
    return (
        await count(world, VPNUser),
        await count(world, Subscription),
        await count(world, AccessGrant),
        await count(world, VPNCredential),
    )


# --- основной сценарий ---


async def test_import_creates_user_subscription_grant_and_credential_for_each_client(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    valid_until = future(45)
    job = await run_import(
        api,
        world,
        worker,
        scene,
        [client(scene.keys[0], "Alice", email="alice@example.com"), client(scene.keys[1], "Bob")],
        valid_until=valid_until,
    )
    result = result_of(job)
    assert set(result) == RESULT_KEYS
    assert [item["public_key"] for item in result["imported"]] == scene.keys[:2]
    assert all(set(item) == {"public_key", "user_id", "credential_id"} for item in result["imported"])
    assert result["already"] == result["gone"] == result["unsupported"] == result["conflict"] == []
    assert job.progress == 100 and job.finished_at is not None

    protocol = await app_protocol(world, scene)
    assert protocol is not None and protocol.port == scene.app.port  # строка протокола создана, порт — из конфига
    assert result["protocol_id"] == str(protocol.id)

    alice, bob = await users(world)
    assert (alice.username, alice.email, alice.status) == ("Alice", "alice@example.com", VPNUserStatus.ACTIVE)
    assert (bob.username, bob.email, bob.status) == ("Bob", None, VPNUserStatus.ACTIVE)

    subscriptions = {s.user_id: s for s in await all_of(world, Subscription)}
    grants = {g.subscription_id: g for g in await all_of(world, AccessGrant)}
    credentials = {c.client_public_key: c for c in await credentials_of(world, scene.server.id)}
    now = datetime.now(timezone.utc)
    for index, user in enumerate((alice, bob)):
        subscription = subscriptions[user.id]
        assert subscription.status == AccessGrantStatus.ACTIVE
        assert subscription.valid_until == datetime.fromisoformat(valid_until)
        assert timedelta(0) <= now - subscription.valid_from < timedelta(minutes=5)
        grant = grants[subscription.id]
        assert (grant.status, grant.server_id, grant.protocol_id) == (
            AccessGrantStatus.ACTIVE,
            scene.server.id,
            protocol.id,
        )
        credential = credentials[scene.keys[index]]
        assert credential.access_grant_id == grant.id and credential.revoked_at is None
        assert str(credential.id) == result["imported"][index]["credential_id"]
        assert str(user.id) == result["imported"][index]["user_id"]
        assert credential.source == credential_source("imported")
        assert credential.client_address == f"10.8.1.{index + 2}"  # IPv4 без маски
        assert credential.encrypted_config is None
        assert credential.encrypted_psk is not None
        assert decrypt_secret(credential.encrypted_psk) == scene.app.peers[index].psk
        assert credential.external_name == f"Client {index}"  # имя из clientsTable, а не username из запроса
    assert scene.keys[2] not in credentials  # то, чего не просили, не импортируется


async def test_imported_client_without_name_in_clients_table_has_no_external_name(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=1, named=False)
    result_of(await run_import(api, world, worker, scene, [client(scene.keys[0], "Alice")]))
    (credential,) = await credentials_of(world, scene.server.id)
    assert credential.external_name is None


async def test_imported_client_without_preshared_key_has_no_stored_psk(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=2)
    strip_psk(scene.app, scene.keys[0])
    result_of(await run_import(api, world, worker, scene, [client(scene.keys[0]), client(scene.keys[1], "B")]))
    by_key = {c.client_public_key: c for c in await credentials_of(world, scene.server.id)}
    assert by_key[scene.keys[0]].encrypted_psk is None  # клиент всё равно импортирован
    assert by_key[scene.keys[1]].encrypted_psk is not None


async def test_import_changes_nothing_on_the_server(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    scene = await scene_with_report(world, worker)
    files_before = dict(scene.app.files)
    peers_before = [(p.public_key, p.allowed_ips, p.psk) for p in scene.app.peers]
    scene.fake.commands.clear()
    result_of(await run_import(api, world, worker, scene, [client(k, f"u{i}") for i, k in enumerate(scene.keys)]))
    assert scene.fake.commands, "the import did not read the server"
    assert scene.fake.changing_commands() == [] and scene.fake.writes == [] and scene.fake.copies == []
    assert scene.fake.stage_files() == [] and scene.app.applies == 0
    assert scene.app.files == files_before
    assert [(p.public_key, p.allowed_ips, p.psk) for p in scene.app.peers] == peers_before
    assert scene.fake.containers == {"amnezia-awg2": "running"}


async def test_import_reads_the_server_again_instead_of_trusting_the_discovery_report(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=1)
    scene.app.read_counts.clear()
    result_of(await run_import(api, world, worker, scene, [client(scene.keys[0])]))
    assert scene.app.read_counts.get(scene.app.config_path, 0) >= 1  # конфиг прочитан заново, уже в задаче импорта


async def test_import_uses_the_psk_from_the_server_not_from_the_report(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=1)
    scene.app.peers[0].psk = new_key()  # PSK сменили в приложении после поиска
    scene.app.files[scene.app.config_path] = scene.app.render_config()
    result_of(await run_import(api, world, worker, scene, [client(scene.keys[0])]))
    (credential,) = await credentials_of(world, scene.server.id)
    assert credential.encrypted_psk is not None and decrypt_secret(credential.encrypted_psk) == scene.app.peers[0].psk


async def test_imported_clients_become_in_panel_in_the_discovery_report(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    result_of(await run_import(api, world, worker, scene, [client(scene.keys[0]), client(scene.keys[1], "B")]))
    report = (await api.get(f"/api/v1/servers/{scene.server.id}/discovery")).json()
    (protocol,) = report["protocols"]
    assert {c["public_key"]: c["status"] for c in protocol["clients"]} == {
        scene.keys[0]: "in_panel",
        scene.keys[1]: "in_panel",
        scene.keys[2]: "new",
    }


async def test_import_for_the_v1_container_leaves_the_v2_one_alone(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    server, _, fake = await world.server(install=False)
    v1_key, v2_key = new_key(), new_key()
    v1 = fake.add_amnezia_app("amnezia-awg", peers=[v1_key])
    fake.add_amnezia_app("amnezia-awg2", peers=[v2_key])
    scene = Scene(server, fake, v1, [v1_key], {}, "amnezia-awg")
    await run_discovery(world, worker, server)
    fake.commands.clear()
    result_of(await run_import(api, world, worker, scene, [client(v1_key)], protocol_type="amnezia_app_awg"))
    (protocol,) = await ProtocolRepository(world.session).list_by_server_id(server.id)
    assert protocol.type == ProtocolType.AMNEZIA_APP_AWG
    assert fake.changing_commands() == [] and not any("amnezia-awg2" in cmd for cmd in fake.commands)
    (credential,) = await credentials_of(world, server.id)
    assert credential.client_public_key == v1_key


async def test_existing_protocol_row_is_reused_and_not_duplicated(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    protocol = VPNProtocol(server_id=scene.server.id, type=ProtocolType.AMNEZIA_APP_AWG2, port=scene.app.port)
    world.session.add(protocol)
    await world.session.commit()
    result = result_of(await run_import(api, world, worker, scene, [client(scene.keys[0])]))
    assert result["protocol_id"] == str(protocol.id)
    assert len(await ProtocolRepository(world.session).list_by_server_id(scene.server.id)) == 1


# --- повторный запуск ---


async def test_repeating_the_import_reports_already_and_creates_nothing_new(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    clients = [client(scene.keys[0], "Alice", email="alice@example.com"), client(scene.keys[1], "Bob")]
    first = result_of(await run_import(api, world, worker, scene, clients))
    rows = await panel_rows(world)
    assert rows == (2, 2, 2, 2)

    second = result_of(await run_import(api, world, worker, scene, clients))
    assert second["imported"] == [] and second["already"] == scene.keys[:2]
    assert second["gone"] == second["unsupported"] == second["conflict"] == []
    assert second["protocol_id"] == first["protocol_id"]
    assert await panel_rows(world) == rows  # ни дублей, ни новых пользователей
    assert len(await ProtocolRepository(world.session).list_by_server_id(scene.server.id)) == 1


async def test_key_with_a_revoked_credential_is_not_already_and_is_imported_again(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    """«Уже есть» — только живой credential. Отозванный (пользователь вернул клиента в приложении) не мешает."""
    scene = await scene_with_report(world, worker, count=1)
    result_of(await run_import(api, world, worker, scene, [client(scene.keys[0], "Alice", email="alice@example.com")]))
    (old,) = await credentials_of(world, scene.server.id)
    old.revoked_at = datetime.now(timezone.utc)
    await world.session.commit()

    result = result_of(
        await run_import(api, world, worker, scene, [client(scene.keys[0], "Alice2", email="a2@example.com")])
    )
    assert result["already"] == [] and [i["public_key"] for i in result["imported"]] == [scene.keys[0]]
    live = [c for c in await credentials_of(world, scene.server.id) if c.revoked_at is None]
    assert len(live) == 1 and live[0].id != old.id


# --- gone / unsupported / already / conflict ---


async def test_result_sorts_clients_into_imported_already_gone_unsupported_and_conflict(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=5)
    k_new, k_gone, k_wide, k_done, k_conflict = scene.keys
    result_of(await run_import(api, world, worker, scene, [client(k_done, "Done")]))  # k_done уже в панели
    taken = await api.post("/api/v1/vpn-users", json={"username": "Taken", "email": "taken@example.com"})
    assert taken.status_code == 201
    drop_peer(scene.app, k_gone)  # клиента удалили в приложении после поиска
    set_allowed_ips(scene.app, k_wide, "10.8.1.4/32, 10.8.1.40/32")  # а этому добавили адрес

    job = await run_import(
        api,
        world,
        worker,
        scene,
        [
            client(k_new, "New"),
            client(k_gone, "Gone"),
            client(k_wide, "Wide"),
            client(k_done, "Done"),
            client(k_conflict, "Conflict", email="taken@example.com"),
        ],
    )
    result = result_of(job)  # конфликт одного клиента не делает задачу проваленной
    assert [i["public_key"] for i in result["imported"]] == [k_new]
    assert result["gone"] == [k_gone] and result["unsupported"] == [k_wide] and result["already"] == [k_done]
    (conflict,) = result["conflict"]
    assert conflict["public_key"] == k_conflict and isinstance(conflict["reason"], str) and conflict["reason"]
    assert set(conflict) == {"public_key", "reason"}
    assert not any(secret in json.dumps(result) for secret in scene.secrets)
    credentials = {c.client_public_key for c in await credentials_of(world, scene.server.id)}
    assert credentials == {k_done, k_new}
    assert {u.username for u in await users(world)} == {"Taken", "Done", "New"}  # у конфликтного клиента нет следов


async def test_client_missing_on_the_server_is_gone_and_creates_nothing(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=1)
    drop_peer(scene.app, scene.keys[0])
    result = result_of(await run_import(api, world, worker, scene, [client(scene.keys[0], "Ghost")]))
    assert result["gone"] == scene.keys and result["imported"] == []
    assert await panel_rows(world) == (0, 0, 0, 0)


async def test_unsupported_peer_is_reported_and_creates_nothing(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=1)
    set_allowed_ips(scene.app, scene.keys[0], "10.8.1.2/32, 10.8.1.60/32")
    result = result_of(await run_import(api, world, worker, scene, [client(scene.keys[0], "Wide")]))
    assert result["unsupported"] == scene.keys and result["imported"] == []
    assert await panel_rows(world) == (0, 0, 0, 0)


# --- пользователи и подписки ---


async def create_user(api: httpx.AsyncClient, username: str, email: str) -> dict[str, Any]:
    response = await api.post("/api/v1/vpn-users", json={"username": username, "email": email})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


@pytest.mark.parametrize("activated", [False, True], ids=["pending-subscription", "active-subscription"])
async def test_import_for_an_existing_user_reuses_the_live_subscription(
    activated: bool, api: httpx.AsyncClient, world: World, worker: JobWorker, admin: AdminLogin
) -> None:
    scene = await scene_with_report(world, worker, count=1)
    known = await create_user(api, "Known", "known@example.com")
    now = datetime.now(timezone.utc)
    service = SubscriptionService(world.session, SYSTEM_ACTOR)
    subscription = await service.create(
        UUID(known["id"]), SubscriptionCreate(valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=5))
    )
    if activated:
        await service.activate(subscription.id)
    valid_until_before = subscription.valid_until

    job = await run_import(
        api,
        world,
        worker,
        scene,
        [client(scene.keys[0], "Ignored name", user_id=known["id"])],
        valid_until=future(100),
    )
    result = result_of(job)
    assert result["imported"][0]["user_id"] == known["id"]
    (user,) = await users(world)  # нового пользователя нет, имя и email прежние
    assert (user.username, user.email) == ("Known", "known@example.com")
    (live_subscription,) = await all_of(world, Subscription)  # подписка переиспользована, а не создана вторая
    assert live_subscription.id == subscription.id and live_subscription.valid_until == valid_until_before
    (grant,) = await all_of(world, AccessGrant)
    assert grant.subscription_id == subscription.id and grant.status == AccessGrantStatus.ACTIVE
    (created,) = await world.audit(AuditAction.VPN_USER_CREATED)  # только наша запись через API
    assert created.entity_id == UUID(known["id"]) and "source" not in (created.meta or {})


async def test_import_for_a_user_without_a_live_subscription_creates_a_new_one(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=1)
    known = await create_user(api, "Known", "known@example.com")
    now = datetime.now(timezone.utc)
    old = await SubscriptionService(world.session, SYSTEM_ACTOR).create(
        UUID(known["id"]), SubscriptionCreate(valid_from=now - timedelta(days=9), valid_until=now + timedelta(days=9))
    )
    await SubscriptionService(world.session, SYSTEM_ACTOR).revoke(old.id)  # живой подписки у пользователя нет
    valid_until = future(60)
    result_of(
        await run_import(
            api, world, worker, scene, [client(scene.keys[0], "x", user_id=known["id"])], valid_until=valid_until
        )
    )
    subscriptions = {s.id: s for s in await all_of(world, Subscription)}
    assert len(subscriptions) == 2 and subscriptions[old.id].status == AccessGrantStatus.REVOKED
    (new,) = [s for s in subscriptions.values() if s.id != old.id]
    assert new.status == AccessGrantStatus.ACTIVE and new.valid_until == datetime.fromisoformat(valid_until)


async def test_second_client_of_the_same_user_on_the_same_protocol_is_a_conflict(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    """У пользователя один живой грант на протокол: второй ключ того же пользователя — конфликт, а не падение задачи."""
    scene = await scene_with_report(world, worker, count=3)
    known = await create_user(api, "Known", "known@example.com")
    result = result_of(
        await run_import(
            api,
            world,
            worker,
            scene,
            [
                client(scene.keys[0], "first", user_id=known["id"]),
                client(scene.keys[1], "second", user_id=known["id"]),
                client(scene.keys[2], "third"),
            ],
        )
    )
    assert [i["public_key"] for i in result["imported"]] == [scene.keys[0], scene.keys[2]]
    (conflict,) = result["conflict"]
    assert conflict["public_key"] == scene.keys[1] and conflict["reason"]
    assert {c.client_public_key for c in await credentials_of(world, scene.server.id)} == {scene.keys[0], scene.keys[2]}


async def test_new_user_with_an_email_that_exists_is_a_conflict_without_leftovers(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=2)
    await create_user(api, "Taken", "taken@example.com")
    result = result_of(
        await run_import(
            api,
            world,
            worker,
            scene,
            [
                client(scene.keys[0], "Dup", email="taken@example.com"),
                client(scene.keys[1], "Fine", email="fine@example.com"),
            ],
        )
    )
    assert [c["public_key"] for c in result["conflict"]] == [scene.keys[0]]
    assert [i["public_key"] for i in result["imported"]] == [scene.keys[1]]
    assert await panel_rows(world) == (2, 1, 1, 1)  # Taken + Fine; у Dup ни пользователя, ни подписки, ни гранта


async def test_failure_of_one_client_is_rolled_back_alone_and_the_others_are_imported(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    """Каждый клиент — во вложенной транзакции: сбой на последнем шаге (credential) откатывает и созданного ради него
    пользователя с подпиской и грантом, а клиенты до и после него импортируются."""
    scene = await scene_with_report(world, worker, count=3)
    bad = scene.keys[1]

    def fail_for_bad_key(mapper: Any, connection: Any, target: VPNCredential) -> None:
        if target.client_public_key == bad:
            raise IntegrityError("INSERT INTO vpn_credentials", {}, Exception("injected failure"))

    event.listen(VPNCredential, "before_insert", fail_for_bad_key)
    try:
        job = await run_import(api, world, worker, scene, [client(k, name) for k, name in zip(scene.keys, "ABC")])
    finally:
        event.remove(VPNCredential, "before_insert", fail_for_bad_key)
    result = result_of(job)
    assert [i["public_key"] for i in result["imported"]] == [scene.keys[0], scene.keys[2]]
    assert [c["public_key"] for c in result["conflict"]] == [bad]
    assert {u.username for u in await users(world)} == {"A", "C"}
    assert await panel_rows(world) == (2, 2, 2, 2)


# --- ошибки задачи ---


async def test_stopped_container_fails_the_job_and_creates_nothing(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    scene.fake.containers["amnezia-awg2"] = "exited"
    job = await run_import(api, world, worker, scene, [client(scene.keys[0])])
    assert job.status == JobStatus.FAILED and job.progress == 0 and job.finished_at is not None
    assert "not running" in (job.error or "")
    assert not any(secret in (job.error or "") for secret in scene.secrets)
    assert await panel_rows(world) == (0, 0, 0, 0) and await app_protocol(world, scene) is None


async def test_missing_container_fails_the_job_and_creates_nothing(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    del scene.fake.containers["amnezia-awg2"]
    job = await run_import(api, world, worker, scene, [client(scene.keys[0])])
    assert job.status == JobStatus.FAILED
    assert await panel_rows(world) == (0, 0, 0, 0) and await app_protocol(world, scene) is None


async def test_unreachable_server_fails_the_job(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    scene = await scene_with_report(world, worker)
    scene.fake.reachable = False
    job = await run_import(api, world, worker, scene, [client(scene.keys[0])])
    assert job.status == JobStatus.FAILED and "Connection error" in (job.error or "") and job.result is None
    assert await panel_rows(world) == (0, 0, 0, 0)


async def test_ssh_breaking_midway_fails_the_job_without_secrets_and_creates_nothing(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    from core.exceptions import SSHConnectionError

    scene = await scene_with_report(world, worker)
    original = scene.fake.run

    def breaking(command: str) -> Any:
        if command.startswith("docker exec"):
            raise SSHConnectionError("Connection lost while reading the container")
        return original(command)

    scene.fake.run = breaking  # type: ignore[method-assign]
    job = await run_import(api, world, worker, scene, [client(scene.keys[0])])
    assert job.status == JobStatus.FAILED and "Connection lost" in (job.error or "")
    assert not any(secret in (job.error or "") for secret in scene.secrets)
    assert await panel_rows(world) == (0, 0, 0, 0)
    assert scene.fake.changing_commands() == []


async def test_import_waits_for_the_server_lock(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    scene = await scene_with_report(world, worker, count=1)
    response = await api.post(
        f"/api/v1/servers/{scene.server.id}/import",
        json={"protocol_type": PROTOCOL_TYPE, "valid_until": future(), "clients": [client(scene.keys[0])]},
    )
    job = await world.session.get(Job, UUID(response.json()["id"]))
    assert job is not None
    async with ServerLock(scene.server.id, blocking_timeout=0):
        assert await worker.process_next(timeout=1)  # сервер занят выдачей/отзывом — задача возвращается в очередь
        await world.session.refresh(job)
        assert job.status == JobStatus.PENDING and await JobQueue().size() == 1
        assert await panel_rows(world) == (0, 0, 0, 0)
    assert await drain(worker) == 1
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS


async def test_import_job_without_server_id_is_rejected(world: World, worker: JobWorker) -> None:
    job = Job(type=import_job_type(), server_id=None, params={})
    world.session.add(job)
    await world.session.commit()
    await JobQueue().enqueue(job.id)
    assert await worker.process_next(timeout=1)
    await world.session.refresh(job)
    assert job.status == JobStatus.FAILED and job.started_at is None
    assert job.error == "Job with type 'import_clients' must have server_id"


@pytest.mark.parametrize("params", [None, {}, {"protocol_type": "amnezia_wg", "valid_until": "x", "clients": []}])
async def test_import_job_with_broken_params_fails_cleanly(
    params: dict[str, Any] | None, world: World, worker: JobWorker
) -> None:
    scene = await make_scene(world, count=1)
    job = Job(type=import_job_type(), server_id=scene.server.id, params=params)
    world.session.add(job)
    await world.session.commit()
    await JobQueue().enqueue(job.id)
    assert await worker.process_next(timeout=1)
    await world.session.refresh(job)
    assert job.status == JobStatus.FAILED and job.error  # не RUNNING навсегда и не падение воркера
    assert await panel_rows(world) == (0, 0, 0, 0)
    assert scene.fake.changing_commands() == []


# --- секреты и недоверенный ввод ---


async def test_result_params_and_error_have_no_secrets(api: httpx.AsyncClient, world: World, worker: JobWorker) -> None:
    scene = await scene_with_report(world, worker)
    job = await run_import(api, world, worker, scene, [client(k, f"u{i}") for i, k in enumerate(scene.keys)])
    text = json.dumps({"result": job.result, "params": job.params, "error": job.error, "message": job.message})
    assert not any(secret in text for secret in scene.secrets)
    assert "PrivateKey" not in text and "PresharedKey" not in text
    response = await api.get(f"/api/v1/jobs/{job.id}")  # и то, что видит любой читающий API
    assert not any(secret in response.text for secret in scene.secrets)


async def test_names_and_emails_from_the_request_are_stored_as_data_and_never_reach_server_commands(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker, count=2)
    nasty = ["a'; rm -rf /; echo '", "$(id) `id` && reboot"]
    scene.fake.commands.clear()
    result_of(
        await run_import(
            api,
            world,
            worker,
            scene,
            [client(scene.keys[0], nasty[0], email="x@example.com"), client(scene.keys[1], nasty[1])],
        )
    )
    assert {u.username for u in await users(world)} == set(nasty)  # хранятся как есть, параметризованным запросом
    commands = " ".join(scene.fake.commands)
    assert not any(value in commands for value in nasty) and "x@example.com" not in commands
    assert scene.fake.changing_commands() == []


# --- аудит ---


async def test_audit_has_started_and_completed_with_counters_and_no_names_or_keys(
    api: httpx.AsyncClient, world: World, worker: JobWorker, admin: AdminLogin
) -> None:
    scene = await scene_with_report(world, worker, count=3)
    drop_peer(scene.app, scene.keys[2])
    job = await run_import(
        api,
        world,
        worker,
        scene,
        [
            client(scene.keys[0], "Secret Alice", email="alice-secret@example.com"),
            client(scene.keys[1], "Secret Bob"),
            client(scene.keys[2], "Secret Ghost"),
        ],
    )
    result_of(job)
    (started,) = await world.audit(import_action("started"))
    (completed,) = await world.audit(import_action("completed"))
    assert started.admin_id == completed.admin_id == admin.admin.id  # от имени инициатора, хотя пишет воркер
    assert completed.entity_id == scene.server.id
    meta = completed.meta or {}
    assert meta["protocol_id"] == job.result["protocol_id"]  # type: ignore[index]
    assert {name: meta[name] for name in ("imported", "already", "gone", "unsupported", "conflict")} == {
        "imported": 2,
        "already": 0,
        "gone": 1,
        "unsupported": 0,
        "conflict": 0,
    }
    for log in (started, completed):
        text = json.dumps(log.meta)
        assert "Secret" not in text and "alice-secret" not in text and not any(k in text for k in scene.keys)
        assert not any(secret in text for secret in scene.secrets)
    assert await world.audit(import_action("failed")) == []


async def test_audit_marks_every_created_user_as_imported_and_skips_existing_ones(
    api: httpx.AsyncClient, world: World, worker: JobWorker, admin: AdminLogin
) -> None:
    scene = await scene_with_report(world, worker, count=3)
    known = await create_user(api, "Known", "known@example.com")
    result_of(
        await run_import(
            api,
            world,
            worker,
            scene,
            [
                client(scene.keys[0], "Secret Alice", email="alice-secret@example.com"),
                client(scene.keys[1], "Secret Bob"),
                client(scene.keys[2], "ignored", user_id=known["id"]),
            ],
        )
    )
    created = {log.entity_id: log for log in await world.audit(AuditAction.VPN_USER_CREATED)}
    imported_users = [u for u in await users(world) if u.username != "Known"]
    assert len(imported_users) == 2 and set(created) == {u.id for u in imported_users} | {UUID(known["id"])}
    for user in imported_users:
        log = created[user.id]
        assert log.admin_id == admin.admin.id and (log.meta or {}).get("source") == "import"
        assert "Secret" not in json.dumps(log.meta) and "alice-secret" not in json.dumps(log.meta)  # без имён и email
    assert "source" not in (created[UUID(known["id"])].meta or {})  # пользователя, созданного через API, не трогали


async def test_audit_failed_has_the_error_and_no_secrets(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    scene = await scene_with_report(world, worker)
    scene.fake.containers["amnezia-awg2"] = "exited"
    job = await run_import(api, world, worker, scene, [client(scene.keys[0], "Secret Alice")])
    assert job.status == JobStatus.FAILED
    (failed,) = await world.audit(import_action("failed"))
    meta = failed.meta or {}
    assert "not running" in meta["error"] and failed.entity_id == scene.server.id
    assert "Secret" not in json.dumps(meta) and not any(secret in json.dumps(meta) for secret in scene.secrets)
    assert await world.audit(import_action("completed")) == []
    assert await world.audit(AuditAction.VPN_USER_CREATED) == []


async def test_audit_of_a_job_created_by_the_system_has_no_admin(world: World, worker: JobWorker) -> None:
    """Задача без инициатора (система) тоже пишет started: актор — SYSTEM_ACTOR, admin_id пуст."""
    scene = await scene_with_report(world, worker, count=1)
    params = {
        "protocol_type": PROTOCOL_TYPE,
        "valid_until": future(),
        "clients": [{"public_key": scene.keys[0], "username": "S", "email": None, "user_id": None}],
    }
    job = await JobService(world.session, SYSTEM_ACTOR).create(import_job_type(), server_id=scene.server.id)
    job.params = params
    await world.session.commit()
    await drain(worker)
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS, job.error
    (completed,) = await world.audit(import_action("completed"))
    assert completed.admin_id is None


async def test_audit_records_who_got_which_access_for_every_imported_client(
    api: httpx.AsyncClient, world: World, worker: JobWorker, admin: AdminLogin
) -> None:
    """Job.result со временем чистится: связь «ключ → пользователь → сервер» должна остаться в журнале аудита."""
    scene = await scene_with_report(world, worker, count=2)
    known = await create_user(api, "Known", "known2@example.com")
    result = result_of(
        await run_import(
            api, world, worker, scene, [client(scene.keys[0], "Alice"), client(scene.keys[1], "x", user_id=known["id"])]
        )
    )
    logs = await world.audit(AuditAction.CREDENTIAL_IMPORTED)
    assert {str(log.entity_id) for log in logs} == {item["credential_id"] for item in result["imported"]}
    by_key = {(log.meta or {})["public_key"]: log for log in logs}
    assert set(by_key) == set(scene.keys)
    for item in result["imported"]:
        log = by_key[item["public_key"]]
        assert log.admin_id == admin.admin.id
        assert str((log.meta or {})["user_id"]) == item["user_id"] and str((log.meta or {})["server_id"]) == str(
            scene.server.id
        )
        assert "access_grant_id" in (log.meta or {})
    assert "Alice" not in json.dumps([log.meta for log in logs], default=str)  # имена в журнал не пишем
