"""«Россия напрямую» (этап 24.4) через HTTP и воркер: загрузка и обновление списка, кандидат и подтверждение,
исключения, применение на шлюзе, sync / health / установка, правило планировщика. SSH - фейковый, RIPE - подмена."""

import threading
from ipaddress import IPv4Network
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from core.config import settings
from core.database.postgres import session_factory
from infrastructure.geoip import DownloadError
from infrastructure.workers.locks import ServerLock
from infrastructure.workers.scheduler import Scheduler, build_rules, schedule_geoip_update
from infrastructure.workers.worker import JobWorker
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.geoip import service as geoip_service
from modules.geoip.models.geoip import GeoipList, GeoipStatus
from modules.jobs.models.job import JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.servers.models.server import VPNServer
from tests.fakes.fake_ssh import FakeServer
from tests.fixtures import World
from tests.geoip_support import cidr_file, ripe_file, synthetic_networks
from tests.integration.conftest import drain
from tests.integration.test_gateway_api import delete_protocol, gateway_server, install_gateway
from tests.integration.test_gateway_exits_api import add_exit, exit_server
from tests.integration.test_mvp_flow import run_job

BASE = synthetic_networks(3200)
GEOIP = "/api/v1/geoip"
DIRECT_NET = "5.45.200.0/24"  # публичная сеть вне синтетического списка (он живёт в 20.x.x.x)


async def upload(api: httpx.AsyncClient, text: str) -> httpx.Response:
    return await api.post(f"{GEOIP}/upload", content=text.encode(), headers={"content-type": "text/plain"})


async def overview(api: httpx.AsyncClient) -> dict[str, Any]:
    response = await api.get(GEOIP)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def gateway(api: httpx.AsyncClient, worker: JobWorker, world: World) -> tuple[UUID, FakeServer]:
    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)
    return sid, fake


async def activate_base(api: httpx.AsyncClient, worker: JobWorker) -> dict[str, Any]:
    response = await upload(api, cidr_file(BASE))
    assert response.status_code == 200, response.text
    await drain(worker)
    return dict(response.json())


async def db_lists() -> list[GeoipList]:
    async with session_factory() as session:
        return list((await session.execute(select(GeoipList).order_by(GeoipList.created_at))).scalars())


async def apply_jobs(world: World) -> list[Any]:
    world.session.expire_all()
    return await world.jobs(JobType.GATEWAY_APPLY_RU)


def route_count(fake: FakeServer) -> int:
    return sum(len(routes) for routes in fake.ru_tables.values())


async def audit_actions(*actions: AuditAction) -> list[AuditLog]:
    async with session_factory() as session:
        query = select(AuditLog).where(AuditLog.action.in_(actions)).order_by(AuditLog.created_at)
        return list((await session.execute(query)).scalars())


# --- загрузка и активация -----------------------------------------------------------------------------------------


async def test_the_first_list_becomes_active_and_the_overview_has_no_prefixes(
    api: httpx.AsyncClient, worker: JobWorker
) -> None:
    result = await upload(api, cidr_file(BASE))

    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "active" and body["prefixes"] == len(BASE) and len(body["sha256"]) == 64
    state = await overview(api)
    assert state["candidates"] == []
    assert state["active"]["id"] == body["list_id"] and state["active"]["prefixes_count"] == len(BASE)
    assert state["active"]["status"] == "active" and state["active"]["source"] == "upload"
    assert "data" not in state["active"] and "20.0.0.0/24" not in str(state)  # самих префиксов в ответе нет
    (stored,) = await db_lists()
    assert stored.status == GeoipStatus.ACTIVE and stored.activated_at is not None
    assert stored.data.count("\n") == len(BASE)


async def test_a_ripe_file_is_accepted_and_skipped_lines_are_counted(api: httpx.AsyncClient) -> None:
    result = await upload(api, ripe_file(BASE, extra=["garbage line", "ripencc|RU|ipv4|oops|256|20240101|allocated"]))

    assert result.status_code == 200, result.text
    assert result.json()["status"] == "active" and result.json()["prefixes"] == len(BASE)
    assert result.json()["version"] == "20240101"  # самая поздняя дата записей
    assert result.json()["skipped"] >= 2 and result.json()["rejected"] == 0


async def test_the_same_list_again_is_unchanged_and_applies_nothing(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    await gateway(api, worker, world)
    await activate_base(api, worker)
    queued = len(await apply_jobs(world))

    again = await upload(api, cidr_file(reversed(BASE)))  # другой порядок строк - тот же список

    assert again.status_code == 200 and again.json()["status"] == "unchanged"
    assert len(await db_lists()) == 1 and len(await apply_jobs(world)) == queued


async def test_a_small_change_replaces_the_active_list_by_itself(api: httpx.AsyncClient, worker: JobWorker) -> None:
    first = await activate_base(api, worker)
    changed = [*BASE[100:], *synthetic_networks(100, first=60)]  # 100 из 3200 заменены: около 3%

    second = await upload(api, cidr_file(changed))

    assert second.json()["status"] == "active" and second.json()["list_id"] != first["list_id"]
    statuses = {str(item.id): item.status for item in await db_lists()}
    assert statuses == {first["list_id"]: GeoipStatus.SUPERSEDED, second.json()["list_id"]: GeoipStatus.ACTIVE}
    assert (await overview(api))["active"]["id"] == second.json()["list_id"]


async def test_a_big_change_waits_for_the_admin_and_the_active_list_stays(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    first = await activate_base(api, worker)
    queued, applied = len(await apply_jobs(world)), fake.ru_applies
    bigger = [*BASE, *synthetic_networks(1000, first=70)]  # +31% префиксов

    second = await upload(api, cidr_file(bigger))

    assert second.status_code == 200 and second.json()["status"] == "candidate"
    state = await overview(api)
    assert state["active"]["id"] == first["list_id"]
    assert [item["id"] for item in state["candidates"]] == [second.json()["list_id"]]
    assert state["candidates"][0]["status"] == "candidate" and state["candidates"][0]["activated_at"] is None
    await drain(worker)
    assert len(await apply_jobs(world)) == queued and fake.ru_applies == applied  # на шлюзы кандидат не идёт

    activated = await api.post(f"{GEOIP}/{second.json()['list_id']}/activate")

    assert activated.status_code == 200 and activated.json()["status"] == "active"
    state = await overview(api)
    assert state["active"]["id"] == second.json()["list_id"] and state["candidates"] == []
    await drain(worker)
    assert route_count(fake) == len(bigger) and fake.ru_applies == applied + 1
    assert (await db_lists())[0].status == GeoipStatus.SUPERSEDED
    assert len(await audit_actions(AuditAction.GEOIP_LIST_ACTIVATED)) == 2  # первый список и подтверждённый


async def test_the_same_candidate_is_not_stored_twice_and_a_new_one_replaces_the_waiting(
    api: httpx.AsyncClient, worker: JobWorker
) -> None:
    await activate_base(api, worker)
    bigger = cidr_file([*BASE, *synthetic_networks(1000, first=70)])
    first = await upload(api, bigger)
    again = await upload(api, bigger)
    assert first.json()["status"] == again.json()["status"] == "candidate"
    assert first.json()["list_id"] == again.json()["list_id"]

    other = await upload(api, cidr_file([*BASE, *synthetic_networks(1500, first=80)]))

    assert other.json()["status"] == "candidate"
    state = await overview(api)
    assert [item["id"] for item in state["candidates"]] == [other.json()["list_id"]]
    statuses = [item.status for item in await db_lists()]
    assert sorted(status.value for status in statuses) == ["active", "candidate", "superseded"]


async def test_only_a_candidate_can_be_activated(api: httpx.AsyncClient, worker: JobWorker) -> None:
    active = await activate_base(api, worker)

    assert (await api.post(f"{GEOIP}/{active['list_id']}/activate")).status_code == 409
    assert (await api.post(f"{GEOIP}/{uuid4()}/activate")).status_code == 404
    assert (await api.post(f"{GEOIP}/not-a-uuid/activate")).status_code == 422
    assert (await overview(api))["active"]["id"] == active["list_id"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "garbage\n" * 5000,
        cidr_file(synthetic_networks(500)),  # мало префиксов
        "10.0.0.0/8\n192.168.0.0/16\n127.0.0.0/8\n224.0.0.0/4\n" * 1000,  # только непубличные
        "2a00:1450::/32\n" * 4000,  # IPv6 игнорируется
    ],
    ids=["empty", "garbage", "few-prefixes", "non-public", "ipv6"],
)
async def test_a_bad_list_is_refused_with_422_and_nothing_is_stored(api: httpx.AsyncClient, text: str) -> None:
    response = await upload(api, text)

    assert response.status_code == 422, response.text
    assert await db_lists() == [] and (await overview(api))["active"] is None


async def test_a_list_over_the_limit_is_refused_with_413(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "geoip_upload_max_bytes", 1000)

    response = await upload(api, cidr_file(BASE))

    assert response.status_code == 413
    assert await db_lists() == []


async def test_a_list_without_content_length_is_limited_while_streaming(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "geoip_upload_max_bytes", 1000)

    async def chunks() -> Any:
        for _ in range(10):
            yield b"20.0.0.0/24\n" * 20

    response = await api.post(f"{GEOIP}/upload", content=chunks(), headers={"content-type": "text/plain"})

    assert response.status_code == 413


async def test_the_audit_records_the_list_without_prefixes(api: httpx.AsyncClient, worker: JobWorker) -> None:
    await activate_base(api, worker)

    (updated,) = await audit_actions(AuditAction.GEOIP_LIST_UPDATED)
    (activated,) = await audit_actions(AuditAction.GEOIP_LIST_ACTIVATED)
    assert updated.entity_type == activated.entity_type == "geoip"
    assert (
        updated.meta["count"] == len(BASE) and updated.meta["source"] == "upload" and len(updated.meta["sha256"]) == 64
    )
    assert "20.0.0.0" not in str(updated.meta) and "20.0.0.0" not in str(activated.meta)


# --- обновление с RIPE (задача geoip_update) ----------------------------------------------------------------------


class Downloads:
    """Подмена download_text: что вернуть, чем упасть и с какими аргументами позвали."""

    def __init__(self) -> None:
        self.text = ripe_file(BASE)
        self.error: Exception | None = None
        self.calls: list[tuple[str, int, int]] = []

    async def __call__(self, url: str, max_bytes: int, timeout: int = 120) -> str:
        self.calls.append((url, max_bytes, timeout))
        if self.error is not None:
            raise self.error
        return self.text


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> Downloads:
    fake = Downloads()
    monkeypatch.setattr(geoip_service, "download_text", fake)
    return fake


async def test_update_is_a_global_job_that_downloads_from_the_configured_source(
    api: httpx.AsyncClient, worker: JobWorker, downloads: Downloads
) -> None:
    response = await api.post(f"{GEOIP}/update")

    assert response.status_code == 202, response.text
    assert response.json()["type"] == "geoip_update" and response.json()["server_id"] is None
    job = await run_job(api, worker, response)
    assert job["status"] == "success", job
    assert job["result"]["status"] == "active" and job["result"]["prefixes"] == len(BASE)
    assert downloads.calls == [
        (settings.geoip_source_url, settings.geoip_download_max_bytes, settings.geoip_download_timeout_seconds)
    ]
    assert (await overview(api))["active"]["source"] == "ripe"


async def test_a_second_update_is_refused_while_the_first_is_unfinished(
    api: httpx.AsyncClient, worker: JobWorker, downloads: Downloads
) -> None:
    first = await api.post(f"{GEOIP}/update")
    assert first.status_code == 202

    assert (await api.post(f"{GEOIP}/update")).status_code == 409
    await drain(worker)
    assert (await api.post(f"{GEOIP}/update")).status_code == 202  # первая завершилась - можно снова


async def test_a_failed_download_fails_the_job_and_keeps_the_active_list(
    api: httpx.AsyncClient, worker: JobWorker, downloads: Downloads
) -> None:
    active = await activate_base(api, worker)
    downloads.error = DownloadError("The source answered 503")

    job = await run_job(api, worker, await api.post(f"{GEOIP}/update"))

    assert job["status"] == "failed" and "503" in job["error"]
    assert (await overview(api))["active"]["id"] == active["list_id"]


async def test_a_broken_source_file_fails_the_job_and_stores_nothing(
    api: httpx.AsyncClient, worker: JobWorker, downloads: Downloads
) -> None:
    downloads.text = "<html>502 Bad Gateway</html>\n" * 3000

    job = await run_job(api, worker, await api.post(f"{GEOIP}/update"))

    assert job["status"] == "failed" and job["error"]
    assert await db_lists() == []


async def test_a_big_jump_from_the_source_is_a_candidate_and_the_job_still_succeeds(
    api: httpx.AsyncClient, worker: JobWorker, downloads: Downloads
) -> None:
    await activate_base(api, worker)
    downloads.text = ripe_file(synthetic_networks(4200, first=80))  # +31% к активным 3200

    job = await run_job(api, worker, await api.post(f"{GEOIP}/update"))

    assert job["status"] == "success" and job["result"]["status"] == "candidate"
    assert len((await overview(api))["candidates"]) == 1


# --- исключения -----------------------------------------------------------------------------------------------------


async def add_override(api: httpx.AsyncClient, network: str, action: str = "direct", note: str | None = None) -> Any:
    return await api.post(f"{GEOIP}/overrides", json={"network": network, "action": action, "note": note})


async def test_overrides_are_added_listed_and_removed(api: httpx.AsyncClient) -> None:
    created = await add_override(api, DIRECT_NET, note="my CDN")

    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["network"], body["action"], body["note"]) == (DIRECT_NET, "direct", "my CDN")
    assert (await api.get(f"{GEOIP}/overrides")).json() == [body]
    assert (await add_override(api, DIRECT_NET, "tunnel")).status_code == 409  # одна запись на сеть

    assert (await api.delete(f"{GEOIP}/overrides/{body['id']}")).status_code == 204
    assert (await api.get(f"{GEOIP}/overrides")).json() == []
    assert (await api.delete(f"{GEOIP}/overrides/{body['id']}")).status_code == 404


@pytest.mark.parametrize(
    "network",
    [
        "garbage",
        "",
        "10.0.0.0/8",
        "192.168.1.0/24",
        "127.0.0.1/32",
        "169.254.0.0/16",
        "224.0.0.0/24",
        "0.0.0.0/0",
        "5.0.0.0/7",
        "172.0.0.0/8",  # накрывает сети Docker 172.16.0.0/12
        "192.0.0.0/8",
        "169.0.0.0/8",  # накрывает метаданные облака 169.254.169.254
        "100.0.0.0/8",  # накрывает CGNAT
        "172.0.0.0/9",
        "169.192.0.0/10",
        "172.17.0.0/16",
        "5.0.0.0/8",  # не крупнее /16
        "5.45.0.0/15",
        "5.45.200.1/24",  # хост-биты
        "2a00:1450::/32",
        "5.45.200.0/24; rm -rf /",
        " 5.45.200.0/24\n8.8.8.0/24",
    ],
)
async def test_a_foreign_or_invalid_network_is_refused_with_422(api: httpx.AsyncClient, network: str) -> None:
    response = await add_override(api, network)

    assert response.status_code == 422, response.text
    assert (await api.get(f"{GEOIP}/overrides")).json() == []


async def test_a_bad_action_or_a_long_note_is_refused_with_422(api: httpx.AsyncClient) -> None:
    assert (await add_override(api, DIRECT_NET, "block")).status_code == 422
    assert (await add_override(api, DIRECT_NET, "direct", "x" * 101)).status_code == 422


async def test_overrides_change_the_applied_list_and_queue_an_apply_for_every_gateway(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    second_sid, second = await gateway(api, worker, world)
    await activate_base(api, worker)
    assert route_count(fake) == route_count(second) == len(BASE)

    await add_override(api, DIRECT_NET, "direct")
    await add_override(api, str(BASE[0]), "tunnel")
    await drain(worker)

    for server in (fake, second):
        routes = {route.split(" ")[0] for table in server.ru_tables.values() for route in table}
        assert DIRECT_NET in routes and str(BASE[0]) not in routes and len(routes) == len(BASE)
    removed = (await api.get(f"{GEOIP}/overrides")).json()
    for item in removed:
        await api.delete(f"{GEOIP}/overrides/{item['id']}")
    await drain(worker)
    assert route_count(fake) == route_count(second) == len(BASE)
    assert {job.server_id for job in await apply_jobs(world)} == {sid, second_sid}


async def test_overrides_without_an_active_list_queue_nothing(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    await gateway(api, worker, world)

    assert (await add_override(api, DIRECT_NET)).status_code == 201

    assert await apply_jobs(world) == []


async def test_the_audit_records_overrides(api: httpx.AsyncClient) -> None:
    created = (await add_override(api, DIRECT_NET, "tunnel")).json()
    await api.delete(f"{GEOIP}/overrides/{created['id']}")

    (added,) = await audit_actions(AuditAction.GEOIP_OVERRIDE_ADDED)
    (removed,) = await audit_actions(AuditAction.GEOIP_OVERRIDE_REMOVED)
    assert added.meta == {"network": DIRECT_NET, "action": "tunnel"} == removed.meta


# --- применение на шлюзе ----------------------------------------------------------------------------------------


def ru_url(sid: UUID | str) -> str:
    return f"/api/v1/servers/{sid}/gateway/ru/apply"


async def test_a_new_list_is_applied_to_every_gateway_once(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    other_sid, other = await gateway(api, worker, world)
    await world.server(install=False)  # сервер без шлюза в рассылке не участвует

    await activate_base(api, worker)

    assert route_count(fake) == route_count(other) == len(BASE)
    jobs = await apply_jobs(world)
    assert sorted(str(job.server_id) for job in jobs) == sorted([str(sid), str(other_sid)])
    assert all(job.status == JobStatus.SUCCESS and job.result["routes"] == len(BASE) for job in jobs)
    applied = await audit_actions(AuditAction.GATEWAY_RU_APPLIED)
    assert len(applied) == 2 and all(entry.meta["routes"] == len(BASE) for entry in applied)
    assert "20.0.0.0" not in str([entry.meta for entry in applied])


async def test_unfinished_apply_jobs_are_not_duplicated(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    await gateway(api, worker, world)
    await upload(api, cidr_file(BASE))  # воркер ещё не работал
    await add_override(api, DIRECT_NET)
    await add_override(api, "8.8.8.0/24")

    assert len(await apply_jobs(world)) == 1  # одна незавершённая применит уже актуальный список
    await drain(worker)


async def test_the_apply_endpoint_checks_the_server_the_gateway_and_the_list(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    assert (await api.post(ru_url(uuid4()))).status_code == 404
    plain, _, _ = await world.server()
    assert (await api.post(ru_url(plain.id))).status_code == 409  # шлюза нет
    sid, _ = await gateway(api, worker, world)
    assert (await api.post(ru_url(sid))).status_code == 409  # активного списка нет
    assert await apply_jobs(world) == []


async def test_the_apply_endpoint_queues_a_job_and_refuses_a_duplicate(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)
    fake.ru_tables.clear()  # кто-то стёр маршруты
    applied = fake.ru_applies

    response = await api.post(ru_url(sid))

    assert response.status_code == 202 and response.json()["type"] == "gateway_apply_ru"
    assert response.json()["server_id"] == str(sid)
    assert (await api.post(ru_url(sid))).status_code == 409
    job = await run_job(api, worker, response)
    assert job["status"] == "success" and job["result"]["routes"] == len(BASE)
    assert route_count(fake) == len(BASE) and fake.ru_applies == applied + 1


async def test_a_failed_apply_fails_the_job_audits_it_and_keeps_the_working_table(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)
    working = {table: list(routes) for table, routes in fake.ru_tables.items()}
    fake.ru_apply_fails = True
    await add_override(api, DIRECT_NET)

    await drain(worker)

    failed = [job for job in await apply_jobs(world) if job.status == JobStatus.FAILED]
    assert len(failed) == 1 and failed[0].error
    assert fake.ru_tables == working
    (entry,) = await audit_actions(AuditAction.GATEWAY_RU_APPLY_FAILED)
    assert entry.meta["error"] and "20.0.0.0" not in str(entry.meta)


async def test_the_apply_waits_for_the_server_lock(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)
    response = await api.post(ru_url(sid))
    applied = fake.ru_applies

    async with ServerLock(sid, blocking_timeout=0):
        assert await worker.process_next(timeout=1)
        assert await JobQueue().size() == 1 and fake.ru_applies == applied  # вернули в очередь, шлюз не тронут
    await drain(worker)
    assert fake.ru_applies == applied + 1
    assert (await api.get(f"/api/v1/jobs/{response.json()['id']}")).json()["status"] == "success"


# --- sync / health / установка ------------------------------------------------------------------------------------


def gateway_report(job: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in job["result"]["protocols"] if item["type"] == "amnezia_gateway")


async def test_sync_without_a_list_does_nothing_about_ru(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))

    assert sync["status"] == "success"
    report = gateway_report(sync)
    assert report["ru"] == {"active_list": False} and report["issues"] == []
    assert route_count(fake) == 0


async def test_sync_repairs_an_empty_or_stale_ru_table(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)

    clean = gateway_report(await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync")))
    assert clean["ru"]["applied"] is False and clean["ru"]["routes"] == len(BASE)

    for table in fake.ru_tables:  # шлюз пересоздали: маршруты потерялись
        fake.ru_tables[table] = fake.ru_tables[table][:10]
    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))

    assert sync["status"] == "success"
    assert gateway_report(sync)["ru"]["applied"] is True and gateway_report(sync)["ru"]["routes"] == len(BASE)
    assert route_count(fake) == len(BASE)


async def test_a_failed_ru_repair_is_an_issue_and_does_not_fail_the_sync(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)
    fake.ru_tables.clear()
    fake.ru_apply_fails = True

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))

    assert sync["status"] == "success"
    report = gateway_report(sync)
    assert report["ru"]["error"] is True and any("Russian routes" in issue for issue in report["issues"])


async def test_health_has_no_ru_check_without_a_list_and_reports_the_table_with_one(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)

    async def checks() -> dict[str, dict[str, Any]]:
        health = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/health"))
        assert health["status"] == "success", health
        gateway_health = next(item for item in health["result"]["protocols"] if item["type"] == "amnezia_gateway")
        return {check["name"]: check for check in gateway_health["checks"]}

    assert "ru" not in await checks()  # списка нет - проверки нет, сервер не DEGRADED из-за неё
    await activate_base(api, worker)
    present = await checks()
    assert present["ru"]["ok"] is True and str(len(BASE)) in present["ru"]["detail"]
    fake.ru_tables.clear()
    assert (await checks())["ru"]["ok"] is False


async def test_reinstalling_the_gateway_brings_the_russian_routes_back(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)
    removed = await run_job(api, worker, await delete_protocol(api, sid, {"type": "amnezia_gateway"}))
    assert removed["status"] == "success", removed
    assert route_count(fake) == 0

    await install_gateway(api, worker, sid)

    assert route_count(fake) == len(BASE)


# --- планировщик -----------------------------------------------------------------------------------------------------


async def test_the_weekly_rule_is_in_the_rule_table() -> None:
    (rule,) = [item for item in build_rules(settings) if item.name == "geoip"]

    assert rule.interval == settings.geoip_update_interval_seconds == 7 * 24 * 3600
    assert rule.action is schedule_geoip_update
    scheduler = Scheduler([rule], tick_seconds=1)
    assert [item.name for item in scheduler.rules] == ["geoip"]


async def test_the_geoip_rule_does_nothing_without_gateways(world: World, worker: JobWorker) -> None:
    await world.server()  # обычный сервер

    assert await schedule_geoip_update() == "no gateways, geoip update skipped"
    assert await world.jobs(JobType.GEOIP_UPDATE) == []


async def test_the_geoip_rule_queues_one_update_while_a_gateway_exists(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    await gateway(api, worker, world)

    world.session.expire_all()
    finished = len(
        await world.jobs(JobType.GEOIP_UPDATE)
    )  # установку шлюза без списка уже сопровождал один GEOIP_UPDATE

    assert await schedule_geoip_update() == "queued geoip update"
    assert await schedule_geoip_update() == "geoip update is already running"
    world.session.expire_all()
    jobs = await world.jobs(JobType.GEOIP_UPDATE)
    pending = [job for job in jobs if job.status == JobStatus.PENDING]
    assert len(jobs) == finished + 1 and len(pending) == 1 and pending[0].server_id is None


def test_ipv4_networks_of_the_fixtures_are_what_the_tests_assume() -> None:
    assert IPv4Network(DIRECT_NET) not in set(BASE) and len(set(BASE)) == len(BASE)


# --- ревью 24.4: зарезервированные диапазоны, хосты, порог, гонки, отпечаток, flock -------------------------------


async def set_host(world: World, sid: UUID, host: str) -> None:
    server = await world.session.get(VPNServer, sid)
    assert server is not None
    server.host = host
    await world.session.commit()


def covered(fake: FakeServer, address: str) -> bool:
    from ipaddress import IPv4Address

    target = IPv4Address(address)
    return any(target in IPv4Network(route.split(" ")[0]) for routes in fake.ru_tables.values() for route in routes)


async def test_a_tunnel_override_for_a_reserved_range_is_allowed_and_changes_nothing(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    _, fake = await gateway(api, worker, world)
    await activate_base(api, worker)

    response = await add_override(api, "10.0.0.0/8", "tunnel")
    await drain(worker)

    assert response.status_code == 201
    assert route_count(fake) == len(BASE)


async def test_a_direct_override_covering_a_gateway_or_exit_address_is_refused(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, _ = await gateway(api, worker, world)
    await set_host(world, sid, "77.88.8.7")
    exit_srv, _, _ = await exit_server(world, "5.45.200.10")
    await add_exit(api, worker, sid, exit_srv.id)

    for network in ("77.88.8.0/24", "77.88.0.0/16", "5.45.200.0/24", "5.45.0.0/16"):
        response = await add_override(api, network, "direct")
        assert response.status_code == 422 and "gateway or an exit" in response.json()["detail"], network
    assert (await add_override(api, "77.88.9.0/24", "direct")).status_code == 201  # рядом - можно
    assert (await add_override(api, "77.88.8.0/24", "tunnel")).status_code == 201  # TUNNEL только вычитает


async def test_addresses_of_the_gateway_and_its_exit_are_cut_out_of_the_applied_list(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    await set_host(world, sid, "20.0.0.5")  # внутри первой сети синтетического списка
    exit_srv, _, _ = await exit_server(world, "20.0.2.9")
    await add_exit(api, worker, sid, exit_srv.id)
    await activate_base(api, worker)
    other_sid, other = await gateway(api, worker, world)  # другой шлюз: его хост (203.0.113.10) в списке не лежит
    await drain(worker)

    assert not covered(fake, "20.0.0.5") and not covered(fake, "20.0.2.9")
    assert covered(fake, "20.0.0.6") and covered(fake, "20.0.2.10") and covered(fake, "20.0.4.1")
    assert covered(other, "20.0.0.5") and covered(other, "20.0.2.9")  # чужие адреса этому шлюзу не мешают
    job = await run_job(api, worker, await api.post(ru_url(sid)))
    assert job["status"] == "success" and job["result"]["prefixes"] == route_count(fake)


async def test_a_reserved_network_in_the_source_is_counted_not_fatal(
    api: httpx.AsyncClient, worker: JobWorker, downloads: Downloads
) -> None:
    downloads.text = ripe_file(
        BASE,
        extra=[
            "ripencc|RU|ipv4|172.0.0.0|16777216|20240101|allocated",
            "ripencc|RU|ipv4|169.254.0.0|65536|20240101|allocated",
            "ripencc|RU|ipv4|100.64.0.0|256|20240101|assigned",
        ],
    )

    job = await run_job(api, worker, await api.post(f"{GEOIP}/update"))

    assert job["status"] == "success" and job["result"]["rejected"] == 3 and job["result"]["status"] == "active"
    assert (await overview(api))["active"]["prefixes_count"] == len(BASE)


async def test_an_upload_with_a_reserved_network_is_refused_as_a_whole(api: httpx.AsyncClient) -> None:
    for line in ("172.0.0.0/8", "169.254.169.0/24", "10.0.0.0/8", "5.0.0.0/8"):
        response = await upload(api, line + "\n" + cidr_file(BASE))
        assert response.status_code == 422 and "cannot be routed directly" in response.text, line
    assert await db_lists() == []
    assert (
        await upload(api, ripe_file(BASE, extra=["ripencc|RU|ipv4|10.0.0.0|256|20240101|allocated"]))
    ).status_code == 422


async def test_replacing_part_of_the_list_with_foreign_networks_at_the_same_size_needs_confirmation(
    api: httpx.AsyncClient, worker: JobWorker
) -> None:
    first = await activate_base(api, worker)
    swapped = [*BASE[1000:], *synthetic_networks(1000, first=90)]  # столько же префиксов и адресов, 31% чужих

    response = await upload(api, cidr_file(swapped))

    assert response.json()["status"] == "candidate"
    assert (await overview(api))["active"]["id"] == first["list_id"]


async def test_the_same_list_supersedes_a_waiting_candidate(api: httpx.AsyncClient, worker: JobWorker) -> None:
    await activate_base(api, worker)
    waiting = await upload(api, cidr_file([*BASE, *synthetic_networks(1500, first=70)]))
    assert waiting.json()["status"] == "candidate" and len((await overview(api))["candidates"]) == 1

    again = await upload(api, cidr_file(BASE))

    assert again.json()["status"] == "unchanged"
    assert (await overview(api))["candidates"] == []
    assert sorted(item.status.value for item in await db_lists()) == ["active", "superseded"]


async def test_a_concurrent_first_list_loses_to_the_unique_index_with_409(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Между чтением «списка нет» и вставкой чужой запрос успел сделать свой список активным: второй получает 409."""
    original_add = geoip_service.GeoipRepository.add

    async def add_after_a_rival(self: Any, geoip_list: GeoipList) -> GeoipList:
        async with session_factory() as other:
            other.add(
                GeoipList(
                    source="upload",
                    version="1",
                    sha256="f" * 64,
                    prefixes_count=1,
                    addresses_count=1,
                    data="5.45.200.0/24\n",
                    status=GeoipStatus.ACTIVE,
                )
            )
            await other.commit()
        return await original_add(self, geoip_list)

    monkeypatch.setattr(geoip_service.GeoipRepository, "add", add_after_a_rival)

    response = await upload(api, cidr_file(BASE))

    assert response.status_code == 409 and "try again" in response.json()["detail"]
    (only,) = await db_lists()
    assert only.sha256 == "f" * 64 and only.status == GeoipStatus.ACTIVE


async def test_the_active_list_is_locked_while_a_list_is_received(
    api: httpx.AsyncClient, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    await activate_base(api, worker)
    calls: list[bool] = []
    original = geoip_service.GeoipRepository.get_active

    async def spy(self: Any, *, lock: bool = False) -> Any:
        calls.append(lock)
        return await original(self, lock=lock)

    monkeypatch.setattr(geoip_service.GeoipRepository, "get_active", spy)

    await upload(api, cidr_file([*BASE[10:], *synthetic_networks(10, first=60)]))

    assert calls[0] is True  # приём начинается с блокировки активного списка (SELECT ... FOR UPDATE)


async def test_parsing_does_not_run_on_the_event_loop_thread(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []
    original = geoip_service._prepare

    def spy(text: str, active_data: str | None, strict: bool) -> Any:
        seen.append(threading.get_ident())
        return original(text, active_data, strict)

    monkeypatch.setattr(geoip_service, "_prepare", spy)

    assert (await upload(api, cidr_file(BASE))).status_code == 200

    assert seen and seen[0] != loop_thread


def test_the_router_does_not_reach_into_the_repository() -> None:
    from pathlib import Path

    source = Path(geoip_service.__file__).parent.joinpath("api", "v1", "router.py").read_text(encoding="utf-8")
    assert "service.repository" not in source and ".repository." not in source


# --- sync / health: отпечаток, испорченный список --------------------------------------------------------------------


async def test_sync_reapplies_when_the_applied_digest_differs_even_with_the_same_route_count(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)
    from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT, ru_applied_file

    fake.files[ru_applied_file(GATEWAY_LAYOUT)] = "e" * 64 + "\n"  # на шлюзе применён другой список того же размера
    applied = fake.ru_applies

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))

    assert sync["status"] == "success" and gateway_report(sync)["ru"]["applied"] is True
    assert fake.ru_applies == applied + 1 and route_count(fake) == len(BASE)
    again = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))
    assert gateway_report(again)["ru"]["applied"] is False  # теперь отпечаток совпал


async def test_health_notices_a_different_list_of_the_same_size(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)
    from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT, ru_applied_file

    fake.files[ru_applied_file(GATEWAY_LAYOUT)] = "e" * 64 + "\n"

    health = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/health"))

    gateway_health = next(item for item in health["result"]["protocols"] if item["type"] == "amnezia_gateway")
    ru = next(check for check in gateway_health["checks"] if check["name"] == "ru")
    assert ru["ok"] is False and "expected" in ru["detail"]


async def test_a_damaged_stored_list_is_an_issue_not_a_crash(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, fake = await gateway(api, worker, world)
    await activate_base(api, worker)
    async with session_factory() as session:
        row = (await session.execute(select(GeoipList))).scalar_one()
        row.data = "SECRET-GARBAGE\n10.0.0.0/8\n"
        await session.commit()

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))
    health = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/health"))
    apply = await run_job(api, worker, await api.post(ru_url(sid)))

    assert sync["status"] == "success" and gateway_report(sync)["ru"]["error"] is True
    assert any("cannot be used" in issue or "damaged" in issue for issue in gateway_report(sync)["issues"])
    gateway_health = next(item for item in health["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert next(check for check in gateway_health["checks"] if check["name"] == "ru")["ok"] is False
    assert apply["status"] == "failed" and "SECRET-GARBAGE" not in str(apply) and "SECRET-GARBAGE" not in str(sync)


# --- установка шлюза: список ещё не получен ------------------------------------------------------------------------


async def test_installing_a_gateway_without_a_list_queues_one_update(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    sid, _ = await gateway_server(world)
    response = await api.post(f"/api/v1/servers/{sid}/protocol", json={"type": "amnezia_gateway"})
    assert response.status_code == 202
    assert await world.jobs(JobType.GEOIP_UPDATE) == []
    await drain(worker)  # установка; хук ставит GEOIP_UPDATE, воркер выполняет (скачивание в тестах отключено)

    world.session.expire_all()
    jobs = await world.jobs(JobType.GEOIP_UPDATE)
    assert len(jobs) == 1 and jobs[0].server_id is None and jobs[0].status == JobStatus.FAILED


async def test_the_install_hook_does_not_duplicate_an_unfinished_update_and_skips_it_with_a_list(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    from modules.audit.actor import SYSTEM_ACTOR
    from modules.gateway.ru import GatewayRuService

    sid, _ = await gateway(api, worker, world)
    world.session.expire_all()
    assert len(await world.jobs(JobType.GEOIP_UPDATE)) == 1
    async with session_factory() as session:
        service = GatewayRuService(session, SYSTEM_ACTOR)
        await service.geoip.schedule_update()  # незавершённая задача есть
        await service.ensure_server(sid)  # хук не ставит вторую
    world.session.expire_all()
    assert len(await world.jobs(JobType.GEOIP_UPDATE)) == 2  # 1 завершённая (FAILED) + 1 поставленная выше
    await drain(worker)

    await activate_base(api, worker)
    before = len(await world.jobs(JobType.GEOIP_UPDATE))
    async with session_factory() as session:
        await GatewayRuService(session, SYSTEM_ACTOR).ensure_server(sid)
    world.session.expire_all()
    assert len(await world.jobs(JobType.GEOIP_UPDATE)) == before  # список есть - обновление не нужно
