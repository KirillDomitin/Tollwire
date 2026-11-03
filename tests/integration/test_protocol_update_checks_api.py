"""Availability API reads cache with servers:read; explicit checks use a separate read-only job."""

import importlib
import importlib.util
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.workers.worker import JobWorker
from modules.auth.models.admin import AdminRole
from modules.auth.permissions import Permission
from modules.jobs.models.job import Job
from tests.fixtures import World
from tests.integration.conftest import drain, login_client, make_admin
from tests.integration.test_rbac import API_ROUTES, required_permissions


def path(server_id: object) -> str:
    return f"/api/v1/servers/{server_id}/protocol/updates"


def test_availability_routes_have_read_and_check_permissions() -> None:
    expected = {
        ("GET", "/api/v1/servers/{server_id}/protocol/updates"): Permission.SERVERS_READ,
        ("POST", "/api/v1/servers/{server_id}/protocol/updates/check"): Permission.SERVERS_CHECK,
    }
    declared = {(method, url): required_permissions(route) for method, url, route in API_ROUTES}
    assert {key: declared.get(key) for key in expected} == {key: [permission] for key, permission in expected.items()}


@pytest.mark.parametrize("role", list(AdminRole))
async def test_cached_read_is_available_to_every_read_role_and_never_touches_ssh(
    session: AsyncSession, world: World, role: AdminRole
) -> None:
    server, protocol, fake = await world.server()
    before = list(fake.commands)
    async with login_client(await make_admin(session, role)) as api:
        response = await api.get(path(server.id))
    assert response.status_code == 200, response.text
    assert fake.commands == before
    item = next(item for item in response.json()["protocols"] if item["protocol_id"] == str(protocol.id))
    assert item["status"] == "unknown" and item["checked_at"] is None


@pytest.mark.parametrize("role", list(AdminRole))
async def test_check_now_permission_differs_from_install_or_update_permission(
    session: AsyncSession, world: World, role: AdminRole
) -> None:
    server, _, fake = await world.server()
    before = list(fake.commands)
    async with login_client(await make_admin(session, role)) as api:
        response = await api.post(path(server.id) + "/check")
    assert response.status_code == (403 if role == AdminRole.VIEWER else 202), response.text
    if response.status_code == 202:
        assert response.json()["type"] == "check_protocol_updates"
    assert fake.commands == before


async def test_check_job_is_deduplicated_for_same_server(api: httpx.AsyncClient, world: World) -> None:
    server, _, _ = await world.server()
    first = await api.post(path(server.id) + "/check")
    assert first.status_code == 202, first.text
    assert (await api.post(path(server.id) + "/check")).status_code == 409


async def test_check_worker_dispatches_read_only_service_and_returns_observations(
    api: httpx.AsyncClient, world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "modules.servers.protocol_updates"
    assert importlib.util.find_spec(name) is not None, "Protocol update check service is required"
    checks = importlib.import_module(name)
    server, protocol, fake = await world.server()
    calls: list[UUID] = []
    result = {
        "protocols": [
            {
                "protocol_id": str(protocol.id),
                "protocol_type": "amnezia_wg",
                "status": "current",
                "stale": False,
                "checked_at": "2026-11-04T00:00:00Z",
                "last_attempt_at": "2026-11-04T00:00:00Z",
                "current_image_id": "sha256:" + "a" * 64,
                "target_image_id": "sha256:" + "a" * 64,
                "target_digest": "amneziavpn/amneziawg-go@sha256:" + "b" * 64,
                "reason": None,
            }
        ]
    }

    async def checked(self: Any, sid: UUID) -> dict[str, Any]:
        calls.append(sid)
        return result

    monkeypatch.setattr(checks.ProtocolUpdateCheckService, "check", checked)
    before = list(fake.commands)
    response = await api.post(path(server.id) + "/check")
    assert response.status_code == 202, response.text
    await drain(worker)
    job = await world.session.get(Job, UUID(response.json()["id"]))
    assert job is not None
    await world.session.refresh(job)
    assert job.status.value == "success" and job.result == result
    assert calls == [server.id] and fake.commands == before
