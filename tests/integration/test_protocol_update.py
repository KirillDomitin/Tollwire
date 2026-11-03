"""Update endpoint/worker contract. Only SSH is fake; HTTP, DB, queue and RBAC are real."""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.workers.worker import JobWorker
from modules.audit.models.audit_log import AuditLog
from modules.auth.models.admin import AdminRole
from modules.auth.permissions import Permission
from modules.jobs.models.job import Job
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from tests.fixtures import World
from tests.integration.conftest import drain, login_client, make_admin
from tests.integration.test_rbac import API_ROUTES, required_permissions

RESULT = {
    "changed": True,
    "previous_image_id": "sha256:" + "a" * 64,
    "target_image_id": "sha256:" + "c" * 64,
    "target_digest": "amneziavpn/amneziawg-go@sha256:" + "b" * 64,
    "rollback": "not_needed",
    "recovered": False,
}


def path(sid: object) -> str:
    return f"/api/v1/servers/{sid}/protocol/update"


def test_update_route_is_in_rbac_matrix_with_manage_permission() -> None:
    routes = [r for method, url, r in API_ROUTES if method == "POST" and url.endswith("/{server_id}/protocol/update")]
    assert len(routes) == 1, "manual protocol update endpoint must exist"
    assert required_permissions(routes[0]) == [Permission.SERVERS_MANAGE]


@pytest.mark.parametrize("role", list(AdminRole))
async def test_update_role_matrix(role: AdminRole, session: AsyncSession, world: World) -> None:
    server, _, fake = await world.server()
    before = len(fake.commands)
    async with login_client(await make_admin(session, role)) as api:
        response = await api.post(path(server.id))
    assert response.status_code == (202 if role == AdminRole.SUPERADMIN else 403), response.text
    assert len(fake.commands) == before  # HTTP only schedules; it never updates through SSH.


@pytest.mark.parametrize("body", [None, {}, {"type": "amnezia_wg"}])
async def test_default_update_job_has_explicit_protocol_type(api: httpx.AsyncClient, world: World, body: Any) -> None:
    server, _, fake = await world.server()
    before = len(fake.commands)
    response = await api.post(path(server.id), **({"json": body} if body is not None else {}))
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["type"] == "update_protocol" and "params" not in data
    job = await world.session.get(Job, UUID(data["id"]))
    assert job is not None and job.params == {"protocol_type": "amnezia_wg"}
    assert len(fake.commands) == before


async def test_query_and_body_must_agree(api: httpx.AsyncClient, world: World) -> None:
    server, _, _ = await world.server()
    response = await api.post(path(server.id), params={"type": "amnezia_gateway"}, json={"type": "amnezia_wg"})
    assert response.status_code == 422


@pytest.mark.parametrize("kind", ["amnezia_app_awg", "amnezia_app_awg2", "unknown", "$(id)"])
async def test_external_or_invalid_protocol_is_rejected(api: httpx.AsyncClient, world: World, kind: str) -> None:
    server, _, _ = await world.server()
    response = await api.post(path(server.id), json={"type": kind})
    assert response.status_code == 422


async def test_missing_server_returns_404(api: httpx.AsyncClient) -> None:
    response = await api.post(path(uuid4()))
    assert response.status_code == 404


async def test_missing_credentials_returns_409(api: httpx.AsyncClient) -> None:
    server = (await api.post("/api/v1/servers", json={"name": "bare-update", "host": "203.0.113.80"})).json()
    response = await api.post(path(server["id"]))
    assert response.status_code == 409


async def test_uninstalled_protocol_is_not_implicitly_installed(api: httpx.AsyncClient, world: World) -> None:
    server, _, fake = await world.server(install=False)
    response = await api.post(path(server.id))
    assert response.status_code == 404
    assert fake.commands == []


async def test_duplicate_pending_update_returns_conflict(api: httpx.AsyncClient, world: World) -> None:
    server, _, _ = await world.server()
    first = await api.post(path(server.id))
    assert first.status_code == 202, first.text
    second = await api.post(path(server.id))
    assert second.status_code == 409


async def test_gateway_query_type_dispatches_gateway_provider(
    api: httpx.AsyncClient, world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infrastructure.vpn import provider

    result_cls = getattr(provider, "ProtocolUpdateResult", None)
    assert result_cls is not None, "Provider update result must exist"
    server, _, _ = await world.server(install=False)
    world.session.add(VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_GATEWAY, port=31337))
    await world.session.commit()
    calls: list[str] = []

    async def updated(self: Any) -> Any:
        calls.append(type(self).__name__)
        return result_cls(**RESULT)

    monkeypatch.setattr(AmneziaGatewayProvider, "update", updated, raising=False)
    response = await api.post(path(server.id), params={"type": "amnezia_gateway"})
    assert response.status_code == 202, response.text
    job = await world.session.get(Job, UUID(response.json()["id"]))
    assert job is not None and job.params == {"protocol_type": "amnezia_gateway"}
    await drain(worker)
    completed = (await api.get(f"/api/v1/jobs/{job.id}")).json()
    assert completed["status"] == "success", completed
    assert calls == ["AmneziaGatewayProvider"]
    assert completed["result"]["protocol_type"] == "amnezia_gateway"


async def test_worker_calls_update_and_preserves_protocol_identity_with_safe_audit(
    api: httpx.AsyncClient, world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infrastructure.vpn import provider

    result_cls = getattr(provider, "ProtocolUpdateResult", None)
    assert result_cls is not None, "Provider update result must be explicit and serializable"
    server, protocol, fake = await world.server()
    assert protocol is not None
    original_id, original_port, before = protocol.id, protocol.port, dict(fake.files)
    called: list[str] = []

    async def updated(self: Any) -> Any:
        called.append(type(self).__name__)
        return result_cls(**RESULT)

    monkeypatch.setattr(AmneziaWGProvider, "update", updated, raising=False)
    response = await api.post(path(server.id))
    assert response.status_code == 202, response.text
    await drain(worker)
    completed = (await api.get(f"/api/v1/jobs/{response.json()['id']}")).json()
    assert completed["status"] == "success", completed
    assert called == ["AmneziaWGProvider"]
    assert RESULT.items() <= completed["result"].items()
    await world.session.refresh(protocol)
    assert (protocol.id, protocol.port) == (original_id, original_port) and fake.files == before
    rows = (await world.session.execute(select(AuditLog).where(AuditLog.entity_id == server.id))).scalars().all()
    actions = {row.action.value for row in rows}
    assert {"protocol_update_started", "protocol_update_completed"} <= actions
    assert "PrivateKey" not in str(completed) + str([row.meta for row in rows])


async def test_failed_update_exposes_rollback_result(
    api: httpx.AsyncClient, world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.ssh_exception import DockerError

    server, _, _ = await world.server()

    async def failed(self: Any) -> Any:
        error = DockerError("Container update failed")
        error.job_result = {**RESULT, "rollback": "succeeded"}
        raise error

    monkeypatch.setattr(AmneziaWGProvider, "update", failed, raising=False)
    response = await api.post(path(server.id))
    assert response.status_code == 202, response.text
    await drain(worker)
    completed = (await api.get(f"/api/v1/jobs/{response.json()['id']}")).json()
    assert completed["status"] == "failed" and completed["result"]["rollback"] == "succeeded"
    rows = (await world.session.execute(select(AuditLog).where(AuditLog.entity_id == server.id))).scalars().all()
    assert "protocol_update_failed" in {row.action.value for row in rows}
