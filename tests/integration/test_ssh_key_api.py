"""POST /servers/{id}/ssh-key через HTTP: постановка задачи, отказы (404/409/422/403) и итог в журнале аудита."""

from typing import Any
from uuid import UUID

import asyncssh
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.workers.worker import JobWorker
from modules.auth.models.admin import AdminRole
from tests.fakes.fake_ssh import FakeServer
from tests.integration.conftest import drain, login_client, make_admin

PASSWORD = "root-password-xyz"
MISSING = "00000000-0000-0000-0000-000000000000"


def host_key() -> str:
    return asyncssh.generate_private_key("ssh-ed25519").convert_to_public().export_public_key().decode()


async def make_server(api: httpx.AsyncClient, *, pinned: bool = True, secret: str | None = PASSWORD) -> str:
    server = (await api.post("/api/v1/servers", json={"name": "srv", "host": "203.0.113.10"})).json()
    sid = str(server["id"])
    if secret:
        assert (await api.put(f"/api/v1/servers/{sid}/credentials", json={"secret": secret})).status_code == 200
    if pinned:
        assert (await api.put(f"/api/v1/servers/{sid}/host-key", json={"public_key": host_key()})).status_code == 200
    return sid


async def audit(api: httpx.AsyncClient, action: str) -> list[dict[str, Any]]:
    logs = (await api.get("/api/v1/audit-logs", params={"limit": 100})).json()
    return [log for log in logs if log["action"] == action]


# --- основной сценарий ------------------------------------------------------------------------------------------------


async def test_post_ssh_key_returns_202_and_job_installs_the_key(api: httpx.AsyncClient, worker: JobWorker) -> None:
    sid = await make_server(api)
    response = await api.post(f"/api/v1/servers/{sid}/ssh-key")
    assert response.status_code == 202
    job = response.json()
    assert (job["type"], job["status"], job["server_id"]) == ("install_ssh_key", "pending", sid)

    await drain(worker)
    done = (await api.get(f"/api/v1/jobs/{job['id']}")).json()
    assert done["status"] == "success", done
    assert set(done["result"]) == {"fingerprint", "username", "replaced", "removed_old"}
    assert (done["result"]["username"], done["result"]["replaced"], done["result"]["removed_old"]) == (
        "root",
        "password",
        0,
    )

    credential = (await api.get(f"/api/v1/servers/{sid}/credentials")).json()
    assert (credential["auth_type"], credential["managed_key"]) == ("ssh_key", True)
    assert credential["key_fingerprint"] == done["result"]["fingerprint"]


async def test_credentials_in_body_are_saved_and_used(api: httpx.AsyncClient, worker: JobWorker) -> None:
    """Диалог «пароль + заменить ключом»: логин и пароль едут в теле, отдельный PUT /credentials не нужен."""
    sid = await make_server(api, secret=None)
    response = await api.post(f"/api/v1/servers/{sid}/ssh-key", json={"username": "deploy", "password": PASSWORD})
    assert response.status_code == 202
    await drain(worker)
    job = (await api.get(f"/api/v1/jobs/{response.json()['id']}")).json()
    assert job["status"] == "success" and job["result"]["username"] == "deploy"
    credential = (await api.get(f"/api/v1/servers/{sid}/credentials")).json()
    assert (credential["username"], credential["auth_type"]) == ("deploy", "ssh_key")


async def test_second_run_rotates_key(api: httpx.AsyncClient, worker: JobWorker) -> None:
    sid = await make_server(api)
    await api.post(f"/api/v1/servers/{sid}/ssh-key")
    await drain(worker)
    first = (await api.get(f"/api/v1/servers/{sid}/credentials")).json()["key_fingerprint"]

    response = await api.post(f"/api/v1/servers/{sid}/ssh-key")
    assert response.status_code == 202  # прежняя задача завершена — можно снова
    await drain(worker)
    job = (await api.get(f"/api/v1/jobs/{response.json()['id']}")).json()
    assert job["status"] == "success" and job["result"]["replaced"] == "ssh_key" and job["result"]["removed_old"] == 1
    assert (await api.get(f"/api/v1/servers/{sid}/credentials")).json()["key_fingerprint"] != first


async def test_audit_started_and_completed_without_secrets(api: httpx.AsyncClient, worker: JobWorker) -> None:
    sid = await make_server(api)
    await api.post(f"/api/v1/servers/{sid}/ssh-key")
    await drain(worker)

    (started,) = await audit(api, "server_ssh_key_install_started")
    (completed,) = await audit(api, "server_ssh_key_install_completed")
    assert started["entity_id"] == completed["entity_id"] == sid
    credential = (await api.get(f"/api/v1/servers/{sid}/credentials")).json()
    assert completed["metadata"]["fingerprint"] == credential["key_fingerprint"]
    assert (completed["metadata"]["username"], completed["metadata"]["replaced"]) == ("root", "password")
    dump = str((await api.get("/api/v1/audit-logs", params={"limit": 100})).json())
    assert PASSWORD not in dump and "PRIVATE KEY" not in dump


async def test_credential_read_has_no_secret_fields(api: httpx.AsyncClient, worker: JobWorker) -> None:
    sid = await make_server(api)
    await api.post(f"/api/v1/servers/{sid}/ssh-key")
    await drain(worker)
    response = await api.get(f"/api/v1/servers/{sid}/credentials")
    assert "secret" not in response.json() and "PRIVATE KEY" not in response.text and PASSWORD not in response.text


# --- сбой на сервере --------------------------------------------------------------------------------------------------


async def test_server_rejecting_new_key_fails_job_and_keeps_password(
    api: httpx.AsyncClient, worker: JobWorker, fakes: dict[UUID, FakeServer]
) -> None:
    sid = await make_server(api)
    fakes[UUID(sid)] = FakeServer(host="203.0.113.10", key_auth_enabled=False)
    response = await api.post(f"/api/v1/servers/{sid}/ssh-key")
    await drain(worker)

    job = (await api.get(f"/api/v1/jobs/{response.json()['id']}")).json()
    assert job["status"] == "failed" and PASSWORD not in str(job)
    credential = (await api.get(f"/api/v1/servers/{sid}/credentials")).json()
    assert (credential["auth_type"], credential["managed_key"], credential["key_fingerprint"]) == (
        "password",
        False,
        None,
    )
    (failed,) = await audit(api, "server_ssh_key_install_failed")
    assert failed["metadata"]["rolled_back"] is True
    assert PASSWORD not in str(failed) and not await audit(api, "server_ssh_key_install_completed")


async def test_failed_job_does_not_block_retry(
    api: httpx.AsyncClient, worker: JobWorker, fakes: dict[UUID, FakeServer]
) -> None:
    sid = await make_server(api)
    fakes[UUID(sid)] = FakeServer(host="203.0.113.10", key_auth_enabled=False)
    await api.post(f"/api/v1/servers/{sid}/ssh-key")
    await drain(worker)
    fakes[UUID(sid)].key_auth_enabled = True
    response = await api.post(f"/api/v1/servers/{sid}/ssh-key")
    assert response.status_code == 202
    await drain(worker)
    assert (await api.get(f"/api/v1/jobs/{response.json()['id']}")).json()["status"] == "success"


# --- отказы -----------------------------------------------------------------------------------------------------------


async def test_unknown_server_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.post(f"/api/v1/servers/{MISSING}/ssh-key")).status_code == 404


async def test_host_key_not_pinned_is_409_and_no_job(api: httpx.AsyncClient) -> None:
    sid = await make_server(api, pinned=False)
    response = await api.post(f"/api/v1/servers/{sid}/ssh-key")
    assert response.status_code == 409
    assert (await api.get("/api/v1/jobs", params={"server_id": sid})).json() == []


async def test_password_in_body_is_not_saved_when_host_key_not_pinned(api: httpx.AsyncClient) -> None:
    sid = await make_server(api, pinned=False, secret=None)
    response = await api.post(f"/api/v1/servers/{sid}/ssh-key", json={"username": "deploy", "password": PASSWORD})
    assert response.status_code == 409 and PASSWORD not in response.text
    assert (await api.get(f"/api/v1/servers/{sid}/credentials")).status_code == 404  # учётка не создана


async def test_no_credentials_is_409(api: httpx.AsyncClient) -> None:
    sid = await make_server(api, secret=None)
    assert (await api.post(f"/api/v1/servers/{sid}/ssh-key")).status_code == 409


async def test_duplicate_while_job_unfinished_is_409(api: httpx.AsyncClient, worker: JobWorker) -> None:
    sid = await make_server(api)
    assert (await api.post(f"/api/v1/servers/{sid}/ssh-key")).status_code == 202
    assert (await api.post(f"/api/v1/servers/{sid}/ssh-key")).status_code == 409
    await drain(worker)
    assert len((await api.get("/api/v1/jobs", params={"server_id": sid})).json()) == 1


@pytest.mark.parametrize("body", [{"username": "deploy"}, {"password": PASSWORD}, {"username": "", "password": "x"}])
async def test_partial_or_invalid_body_is_422(api: httpx.AsyncClient, body: dict[str, str]) -> None:
    """Логин и пароль — оба или ни одного."""
    sid = await make_server(api)
    response = await api.post(f"/api/v1/servers/{sid}/ssh-key", json=body)
    assert response.status_code == 422 and PASSWORD not in response.text


async def test_unsafe_username_is_422(api: httpx.AsyncClient) -> None:
    sid = await make_server(api)
    body = {"username": "root; rm -rf /", "password": PASSWORD}
    assert (await api.post(f"/api/v1/servers/{sid}/ssh-key", json=body)).status_code == 422


@pytest.mark.parametrize("role", [AdminRole.OPERATOR, AdminRole.VIEWER])
async def test_only_servers_manage_can_install_key(
    api: httpx.AsyncClient, session: AsyncSession, role: AdminRole
) -> None:
    sid = await make_server(api)
    async with login_client(await make_admin(session, role)) as http:
        assert (await http.post(f"/api/v1/servers/{sid}/ssh-key")).status_code == 403
    assert (await api.get("/api/v1/jobs", params={"server_id": sid})).json() == []
