"""request_id через HTTP и связка «запрос → задача → воркер» в логах."""

import logging

import httpx
import pytest

from core.log import JsonFormatter
from infrastructure.workers.worker import JobWorker
from tests.fixtures import World
from tests.integration.conftest import AdminLogin, drain


def records(caplog: pytest.LogCaptureFixture, logger: str, msg: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger and r.getMessage() == msg]


async def test_request_id_generated_and_logged(client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    response = await client.get("/health", params={"q": "should-not-be-logged"})
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    (line,) = records(caplog, "api.request", "request")
    assert line.context == {"request_id": request_id}  # type: ignore[attr-defined]
    assert (line.method, line.path, line.status) == ("GET", "/health", 200)  # type: ignore[attr-defined]
    assert "should-not-be-logged" not in JsonFormatter().format(line)


async def test_request_id_from_proxy_is_kept_only_if_safe(client: httpx.AsyncClient) -> None:
    from_nginx = "0123456789abcdef0123456789abcdef"
    assert (await client.get("/health", headers={"X-Request-ID": from_nginx})).headers["x-request-id"] == from_nginx
    forged = (await client.get("/health", headers={"X-Request-ID": "x status=200 fake"})).headers["x-request-id"]
    assert forged != "x status=200 fake" and len(forged) == 32  # мусор заменён своим id


async def test_request_links_to_job_and_worker_logs(
    api: httpx.AsyncClient, worker: JobWorker, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    server, _, _ = await world.server()
    caplog.set_level(logging.INFO)
    response = await api.post(f"/api/v1/servers/{server.id}/health")
    request_id, job_id = response.headers["x-request-id"], response.json()["id"]
    (queued,) = records(caplog, "jobs", "job queued")
    assert queued.context["request_id"] == request_id and queued.job_id == job_id  # type: ignore[attr-defined]

    await drain(worker)
    for msg in ("job started", "job succeeded"):
        (line,) = records(caplog, "worker", msg)
        assert line.context == {  # type: ignore[attr-defined]
            "job_id": job_id,
            "job_type": "health_check",
            "server_id": str(server.id),
        }


async def test_failed_job_logged_without_traceback_for_expected_errors(
    world: World, worker: JobWorker, api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    server, _, fake = await world.server()
    fake.reachable = False
    await api.post(f"/api/v1/servers/{server.id}/sync")
    caplog.set_level(logging.INFO)
    await drain(worker)
    (failed,) = records(caplog, "worker", "job failed")
    assert failed.levelno == logging.WARNING and not failed.exc_info  # без трейсбека
    assert "Connection error" in failed.error  # type: ignore[attr-defined]


async def test_login_secrets_never_reach_logs(
    client: httpx.AsyncClient, admin: AdminLogin, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    response = await client.post("/api/v1/auth/login", json={"email": admin.email, "password": admin.password})
    token = response.json()["access_token"]
    refresh = response.cookies.get("refresh_token") or client.cookies.get("refresh_token")
    await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    dump = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    for secret in (admin.password, token, refresh):
        assert secret and secret not in dump
