"""Надёжность (этап 19.2): ввод → 422/409 вместо 500, гонки на уникальность, воркер и планировщик переживают
ошибки БД и Redis, задачи не зависают, Job.error без секретов, refresh одноразовый и при гонке."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from infrastructure.workers.scheduler import Rule, Scheduler, fail_stale_jobs
from infrastructure.workers.worker import JobWorker
from modules.access.repository import SubscriptionRepository
from modules.audit.actor import SYSTEM_ACTOR
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.jobs.service import JobService
from modules.servers.models.server import ServerStatus, VPNServer
from tests.fixtures import World
from tests.integration.conftest import AdminLogin, drain

NOW = datetime.now(timezone.utc)


async def new_server(api: httpx.AsyncClient, name: str = "srv", host: str = "203.0.113.10") -> str:
    response = await api.post("/api/v1/servers", json={"name": name, "host": host})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# --- ввод: 422 вместо 500 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"name": None},  # NOT NULL — раньше 500 на commit
        {"host": None},
        {"ssh_port": 0},
        {"ssh_port": 70000},
        {"host": "evil host; rm -rf /"},  # только символы имени хоста / IP
        {"name": ""},
        {"description": "x" * 301},
    ],
)
async def test_server_patch_rejects_bad_input(api: httpx.AsyncClient, body: dict[str, Any]) -> None:
    sid = await new_server(api)
    assert (await api.patch(f"/api/v1/servers/{sid}", json=body)).status_code == 422


async def test_nullable_field_can_be_cleared_and_name_must_stay_unique(api: httpx.AsyncClient) -> None:
    sid = await new_server(api)
    await new_server(api, name="taken", host="203.0.113.11")
    assert (await api.patch(f"/api/v1/servers/{sid}", json={"description": None})).status_code == 200
    assert (await api.patch(f"/api/v1/servers/{sid}", json={"name": "taken"})).status_code == 409  # было 500


async def test_credentials_and_vpn_user_validation(api: httpx.AsyncClient) -> None:
    sid = await new_server(api)
    assert (await api.put(f"/api/v1/servers/{sid}/credentials", json={"secret": ""})).status_code == 422
    assert (
        await api.put(f"/api/v1/servers/{sid}/credentials", json={"secret": "pw", "username": "root; id"})
    ).status_code == 422

    a = (await api.post("/api/v1/vpn-users", json={"username": "a", "email": "a@example.com"})).json()
    await api.post("/api/v1/vpn-users", json={"username": "b", "email": "b@example.com"})
    assert (await api.patch(f"/api/v1/vpn-users/{a['id']}", json={"email": None})).status_code == 422
    assert (await api.patch(f"/api/v1/vpn-users/{a['id']}", json={"email": "b@example.com"})).status_code == 409


async def test_subscription_period_is_validated(api: httpx.AsyncClient) -> None:
    user = (await api.post("/api/v1/vpn-users", json={"username": "u", "email": "u@example.com"})).json()
    url = f"/api/v1/vpn-users/{user['id']}/subscriptions"
    start, end = NOW.isoformat(), (NOW + timedelta(days=30)).isoformat()
    assert (await api.post(url, json={"valid_from": end, "valid_until": start})).status_code == 422
    naive = {"valid_from": "2026-10-01T00:00:00", "valid_until": "2026-11-01T00:00:00"}  # без часового пояса
    assert (await api.post(url, json=naive)).status_code == 422
    sub = (await api.post(url, json={"valid_from": start, "valid_until": end})).json()
    # PATCH одной даты, которая ломает период вместе со второй (её в запросе нет), — тоже 422
    early = (NOW - timedelta(days=1)).isoformat()
    assert (await api.patch(f"/api/v1/subscriptions/{sub['id']}", json={"valid_until": early})).status_code == 422


async def test_test_connection_without_credentials_is_409(api: httpx.AsyncClient) -> None:
    sid = await new_server(api)
    response = await api.post(f"/api/v1/servers/{sid}/test-connection")
    assert response.status_code == 409  # было 500


# --- гонки на уникальность -------------------------------------------------------------------------------------------


async def test_check_then_insert_race_is_closed_by_unique_index(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гонка «оба проверили, оба вставили»: второй запрос не видит ещё не закоммиченную подписку первого.
    Воспроизводим её окно детерминированно — проверка в сервисе «ничего не видит». Дальше спасает только
    уникальный индекс БД, а ответ — 409, а не 500. (Одновременные запросы через тестовый транспорт гонку не
    воспроизводили: выполнялись по очереди — мутационная проверка показала, что такой тест ничего не ловил.)"""
    user = (await api.post("/api/v1/vpn-users", json={"username": "u", "email": "u@example.com"})).json()
    url = f"/api/v1/vpn-users/{user['id']}/subscriptions"
    period = {"valid_from": NOW.isoformat(), "valid_until": (NOW + timedelta(days=30)).isoformat()}

    async def sees_nothing(self: Any, user_id: Any) -> None:
        return None

    monkeypatch.setattr(SubscriptionRepository, "get_active_by_user_id", sees_nothing)
    assert (await api.post(url, json=period)).status_code == 201
    second = await api.post(url, json=period)
    assert second.status_code == 409 and second.json() == {"detail": "Conflict with existing data"}
    monkeypatch.undo()
    assert len((await api.get(url)).json()) == 1  # вторая подписка не создана, список отдаётся (не 500)


async def test_concurrent_refresh_issues_only_one_session(client: httpx.AsyncClient, api: httpx.AsyncClient) -> None:
    """Две вкладки обновляют токен одновременно: раньше обе получали новую сессию из одного refresh-токена."""
    codes = sorted(r.status_code for r in await asyncio.gather(*(api.post("/api/v1/auth/refresh") for _ in range(2))))
    assert codes == [200, 401]


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "not-a-uuid", "type": "access"},
        {"type": "access"},  # без sub
        {"sub": "<admin>", "type": "refresh"},  # настоящий админ, но токен не access — пускать нельзя
    ],
)
async def test_malformed_but_signed_token_is_401(
    client: httpx.AsyncClient, admin: AdminLogin, claims: dict[str, str]
) -> None:
    claims = {k: str(admin.admin.id) if v == "<admin>" else v for k, v in claims.items()}
    token = jwt.encode(
        {**claims, "exp": NOW + timedelta(minutes=5)}, settings.jwt_secret_key.get_secret_value(), algorithm="HS256"
    )
    response = await client.get("/api/v1/servers", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401  # было 500 на UUID(...)


# --- живые клиенты защищают сервер от удаления -----------------------------------------------------------------------


async def test_server_with_live_clients_cannot_be_deleted(world: World, api: httpx.AsyncClient) -> None:
    server, protocol, _ = await world.server()
    assert protocol is not None
    await world.issued(protocol)
    assert (await api.delete(f"/api/v1/servers/{server.id}")).status_code == 409  # каскад потерял бы клиентов
    assert (await api.delete(f"/api/v1/servers/{server.id}/protocol")).status_code == 409


# --- воркер и планировщик ---------------------------------------------------------------------------------------------


async def test_db_error_in_job_fails_it_and_discards_partial_changes(
    world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _, _ = await world.server()

    async def broken_sync(self: JobWorker, session: AsyncSession, server_id: Any) -> dict[str, Any]:
        dirty = await session.get(VPNServer, server_id)
        assert dirty is not None
        dirty.description = "half-done change"  # незавершённое изменение до сбоя
        await session.execute(text("SELECT 1/0"))  # ошибка БД: сессия в состоянии «нужен rollback»
        return {}

    monkeypatch.setattr(JobWorker, "sync_server", broken_sync)
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.SYNC_SERVER, server_id=server.id)
    await drain(worker)
    await world.session.refresh(job)
    await world.session.refresh(server)
    assert job.status == JobStatus.FAILED and "division by zero" in (job.error or "")  # не висит в RUNNING
    assert server.description != "half-done change"  # не ушло в базу вместе с FAILED


async def test_job_error_is_redacted(world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch) -> None:
    server, _, _ = await world.server()

    async def leaking_sync(self: JobWorker, session: AsyncSession, server_id: Any) -> dict[str, Any]:
        raise RuntimeError("awg-quick: bad line 'PrivateKey = aGVsbG8gd29ybGQgaGVsbG8gd29ybGQgaGVsbG8=' in config")

    monkeypatch.setattr(JobWorker, "sync_server", leaking_sync)
    job = await JobService(world.session, SYSTEM_ACTOR).create(JobType.SYNC_SERVER, server_id=server.id)
    await drain(worker)
    await world.session.refresh(job)
    assert "aGVsbG8" not in (job.error or "") and "PrivateKey = ***" in (job.error or "")


async def test_worker_survives_redis_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = JobWorker(JobQueue())
    calls = 0

    async def flaky_dequeue(timeout: int = 2) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RedisConnectionError("Redis is restarting")
        worker.stop()  # второй вызов: Redis «вернулся», выходим из цикла
        return None

    monkeypatch.setattr(worker.job_queue, "dequeue", flaky_dequeue)
    await asyncio.wait_for(worker.run(), timeout=10)  # раньше исключение роняло процесс
    assert calls == 2


async def test_scheduler_survives_redis_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def action() -> str:
        calls.append("ran")
        return "ok"

    scheduler = Scheduler([Rule("r", 60, action)], tick_seconds=0)
    attempts = 0

    async def flaky_claim(rule: Rule) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RedisConnectionError("Redis is restarting")
        scheduler.stop()
        return True

    monkeypatch.setattr(scheduler, "_claim", flaky_claim)
    await asyncio.wait_for(scheduler.run(), timeout=10)
    assert attempts == 2 and calls == ["ran"]


async def test_stale_install_marks_server_error(world: World, session: AsyncSession) -> None:
    """Воркер убит посреди установки: сервер не должен навсегда остаться INSTALLING (он выпадает из проверок)."""
    server, _, _ = await world.server(install=False)
    server.status = ServerStatus.INSTALLING
    job = Job(
        type=JobType.INSTALL_SERVER,
        server_id=server.id,
        status=JobStatus.RUNNING,
        started_at=NOW - timedelta(hours=1),
    )
    session.add_all([server, job])
    await session.commit()
    await fail_stale_jobs(1800)
    await session.refresh(server)
    assert server.status == ServerStatus.ERROR
