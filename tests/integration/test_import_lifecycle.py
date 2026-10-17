"""Жизнь импортированного клиента после импорта (этап 23.4): отзыв и истечение снимают peer в приложении, конфиг
недоступен (409), API credential'ов показывает источник, пользователи без email, операции с протоколом приложения не
трогают сервер и не теряют импортированных клиентов.

Импортированный credential создаётся в БД вручную (`imported_credential`): так он выглядит после задачи импорта, а сама
задача проверяется в test_import_job.py."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.vpn.awg_config import list_peers
from infrastructure.workers.worker import JobWorker
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.service import AccessGrantService, SubscriptionService, VPNCredentialService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction
from modules.auth.models.admin import AdminRole
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.vpn_users.models.user import VPNUser, VPNUserStatus
from tests.fakes.fake_ssh import APP_CLIENTS_TABLE
from tests.fixtures import World
from tests.import_support import (
    Scene,
    add_app_protocol,
    config_unavailable_error,
    credential_source,
    drop_peer,
    imported_credential,
    make_scene,
)
from tests.integration.conftest import drain, login_client, make_admin
from tests.integration.discovery_support import run_discovery


@dataclass
class Imported:
    scene: Scene
    protocol: VPNProtocol
    subscription: Subscription
    grant: AccessGrant
    credential: VPNCredential
    user: VPNUser

    @property
    def key(self) -> str:
        return self.credential.client_public_key

    @property
    def other(self) -> str:
        return self.scene.keys[1]


async def imported(world: World, *, container: str = "amnezia-awg2") -> Imported:
    """Сервер с приложением (два клиента), строка протокола и импортированный credential первого клиента."""
    scene = await make_scene(world, count=2, container=container)
    protocol = await add_app_protocol(world, scene)
    user, subscription, grant, credential = await imported_credential(
        world, protocol, scene.keys[0], psk=scene.app.peers[0].psk, address="10.8.1.2", external_name="Client 0"
    )
    return Imported(scene, protocol, subscription, grant, credential, user)


# --- отзыв, истечение ---


async def test_revoking_an_imported_credential_removes_its_peer_and_nothing_else(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    state = await imported(world)
    app = state.scene.app
    other_psk = app.peers[1].psk

    response = await api.post(f"/api/v1/credentials/{state.credential.id}/revoke")
    assert response.status_code == 202 and response.json()["type"] == "revoke_access"
    assert await drain(worker) == 1

    await world.session.refresh(state.credential)
    assert state.credential.revoked_at is not None
    assert [p.public_key for p in list_peers(app.files[app.config_path])] == [state.other]
    assert [p.public_key for p in app.peers] == [state.other] and app.peers[0].psk == other_psk
    assert [e["clientId"] for e in json.loads(app.files[APP_CLIENTS_TABLE])] == [state.other]
    assert state.scene.fake.stage_files() == [] and state.scene.fake.unexpected_changing_commands() == []
    (audit,) = await world.audit(AuditAction.CREDENTIAL_REVOKED, state.credential.id)
    assert (audit.meta or {})["public_key"] == state.key
    assert not any(secret in json.dumps(audit.meta) for secret in state.scene.secrets)


async def test_revoking_an_imported_credential_whose_peer_is_already_gone_succeeds(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    state = await imported(world)
    app = state.scene.app
    drop_peer(app, state.key)  # и peer, и запись clientsTable: иначе revoke чистит «осиротевшую» запись таблицы (23.3)
    state.scene.fake.writes.clear()

    assert (await api.post(f"/api/v1/credentials/{state.credential.id}/revoke")).status_code == 202
    assert await drain(worker) == 1
    await world.session.refresh(state.credential)
    assert state.credential.revoked_at is not None
    assert state.scene.fake.writes == [] and state.scene.fake.copies == []  # снимать уже нечего


@pytest.mark.parametrize("reason", ["grant", "subscription", "expiry"])
async def test_ending_access_of_an_imported_client_removes_its_peer(
    reason: str, world: World, worker: JobWorker
) -> None:
    state = await imported(world)
    app = state.scene.app
    if reason == "grant":
        await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(state.grant.id)
    elif reason == "subscription":
        await SubscriptionService(world.session, SYSTEM_ACTOR).revoke(state.subscription.id)
    else:
        state.subscription.valid_from = datetime.now(timezone.utc) - timedelta(days=3)
        state.subscription.valid_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        await world.session.commit()
        await SubscriptionService(world.session, SYSTEM_ACTOR).expire(state.subscription.id)
    assert await drain(worker) == 1

    await world.session.refresh(state.credential)
    assert state.credential.revoked_at is not None
    assert state.key not in app.files[app.config_path] and state.other in app.files[app.config_path]
    assert [p.public_key for p in app.peers] == [state.other]


async def test_discovery_shows_revoked_in_panel_after_the_imported_credential_is_revoked(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    state = await imported(world)
    await run_discovery(world, worker, state.scene.server)

    def statuses(report: dict[str, Any]) -> dict[str, str]:
        (protocol,) = report["protocols"]
        return {c["public_key"]: c["status"] for c in protocol["clients"]}

    url = f"/api/v1/servers/{state.scene.server.id}/discovery"
    assert statuses((await api.get(url)).json()) == {state.key: "in_panel", state.other: "new"}
    await api.post(f"/api/v1/credentials/{state.credential.id}/revoke")
    await drain(worker)
    assert statuses((await api.get(url)).json()) == {state.key: "revoked_in_panel", state.other: "new"}


# --- конфиг недоступен ---


async def test_config_of_an_imported_credential_is_409_and_is_not_audited_as_viewed(
    api: httpx.AsyncClient, world: World
) -> None:
    state = await imported(world)
    response = await api.post(f"/api/v1/credentials/{state.credential.id}/config")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"].lower()
    assert any(word in detail for word in ("imported", "private key", "импорт", "приватн")), detail
    assert not any(secret in response.text for secret in state.scene.secrets)
    assert await world.audit(AuditAction.CONFIG_VIEWED) == []  # ничего не показали — «просмотра» не было


async def test_service_raises_config_unavailable_for_a_credential_without_config(world: World) -> None:
    from core.exceptions import AppError

    state = await imported(world)
    error_class = config_unavailable_error()
    assert issubclass(error_class, AppError)
    with pytest.raises(error_class):
        await VPNCredentialService(world.session, SYSTEM_ACTOR).reveal_config(state.credential.id)


async def test_viewer_gets_403_and_not_409_for_the_config_of_an_imported_credential(
    session: AsyncSession, world: World
) -> None:
    state = await imported(world)
    async with login_client(await make_admin(session, AdminRole.VIEWER)) as http:
        assert (await http.post(f"/api/v1/credentials/{state.credential.id}/config")).status_code == 403


async def test_config_of_an_issued_credential_is_still_revealed(api: httpx.AsyncClient, world: World) -> None:
    _, protocol, _ = await world.server()
    _, _, _, issued = await world.issued(protocol)
    response = await api.post(f"/api/v1/credentials/{issued.id}/config")
    assert response.status_code == 200 and "[Interface]" in response.json()["config"]


# --- чтение credential ---


async def test_credential_read_shows_source_and_external_name(api: httpx.AsyncClient, world: World) -> None:
    state = await imported(world)
    one = (await api.get(f"/api/v1/credentials/{state.credential.id}")).json()
    assert (one["source"], one["external_name"]) == ("imported", "Client 0")
    listed = (await api.get(f"/api/v1/access/{state.grant.id}/credentials")).json()
    assert [(c["id"], c["source"], c["external_name"]) for c in listed] == [
        (str(state.credential.id), "imported", "Client 0")
    ]
    for secret in (*state.scene.secrets, "encrypted", "psk"):
        assert secret not in json.dumps(one) and secret not in json.dumps(listed)


async def test_credential_read_of_an_issued_credential_is_issued_without_external_name(
    api: httpx.AsyncClient, world: World
) -> None:
    _, protocol, _ = await world.server()
    _, _, grant, issued = await world.issued(protocol)
    one = (await api.get(f"/api/v1/credentials/{issued.id}")).json()
    assert (one["source"], one["external_name"]) == ("issued", None)
    (listed,) = (await api.get(f"/api/v1/access/{grant.id}/credentials")).json()
    assert (listed["source"], listed["external_name"]) == ("issued", None)
    await world.session.refresh(issued)
    assert issued.source == credential_source("issued")  # по умолчанию


# --- пользователи без email ---


async def make_user_without_email(world: World, username: str = "imported") -> VPNUser:
    user = VPNUser(username=username, email=None, status=VPNUserStatus.ACTIVE)
    world.session.add(user)
    await world.session.commit()
    return user


async def test_user_without_email_is_listed_and_read_with_null_email(api: httpx.AsyncClient, world: World) -> None:
    first, second = await make_user_without_email(world, "one"), await make_user_without_email(world, "two")
    listed = {u["id"]: u for u in (await api.get("/api/v1/vpn-users")).json()}
    assert listed[str(first.id)]["email"] is None and listed[str(second.id)]["email"] is None  # несколько NULL
    one = await api.get(f"/api/v1/vpn-users/{first.id}")
    assert one.status_code == 200 and one.json()["email"] is None


async def test_user_without_email_can_be_edited_and_given_an_email(api: httpx.AsyncClient, world: World) -> None:
    user = await make_user_without_email(world)
    renamed = await api.patch(f"/api/v1/vpn-users/{user.id}", json={"username": "Renamed"})
    assert renamed.status_code == 200 and renamed.json()["email"] is None
    with_email = await api.patch(f"/api/v1/vpn-users/{user.id}", json={"email": "now@example.com"})
    assert with_email.status_code == 200 and with_email.json()["email"] == "now@example.com"


async def test_email_cannot_be_cleared_or_made_invalid_through_the_api(api: httpx.AsyncClient, world: World) -> None:
    user = await make_user_without_email(world)
    assert (await api.patch(f"/api/v1/vpn-users/{user.id}", json={"email": None})).status_code == 422
    assert (await api.patch(f"/api/v1/vpn-users/{user.id}", json={"email": "nope"})).status_code == 422


async def test_creating_a_user_through_the_api_still_requires_an_email(api: httpx.AsyncClient) -> None:
    assert (await api.post("/api/v1/vpn-users", json={"username": "x"})).status_code == 422
    assert (await api.post("/api/v1/vpn-users", json={"username": "x", "email": None})).status_code == 422
    assert (await api.post("/api/v1/vpn-users", json={"username": "x", "email": "bad"})).status_code == 422
    assert (await api.post("/api/v1/vpn-users", json={"username": "x", "email": "ok@example.com"})).status_code == 201


async def test_user_with_a_live_imported_client_cannot_be_deleted_until_it_is_revoked(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    state = await imported(world)
    await world.session.execute(update(VPNUser).where(VPNUser.id == state.user.id).values(email=None))
    await world.session.commit()
    assert (await api.delete(f"/api/v1/vpn-users/{state.user.id}")).status_code == 409  # peer остался бы на сервере
    await api.post(f"/api/v1/credentials/{state.credential.id}/revoke")
    await drain(worker)
    assert (await api.delete(f"/api/v1/vpn-users/{state.user.id}")).status_code == 204


# --- протокол приложения: операции API его не трогают ---


async def test_removing_the_protocol_is_refused_while_imported_clients_are_live(
    api: httpx.AsyncClient, world: World
) -> None:
    state = await imported(world)
    response = await api.delete(f"/api/v1/servers/{state.scene.server.id}/protocol")
    assert response.status_code == 409 and "live" in response.json()["detail"]
    assert await world.jobs(JobType.REMOVE_PROTOCOL) == []
    await world.session.refresh(state.credential)
    assert state.credential.revoked_at is None
    assert await ProtocolRepository(world.session).get_by_id(state.protocol.id) is not None  # доступы не уйдут каскадом


@pytest.mark.parametrize("container", ["amnezia-awg", "amnezia-awg2"])
async def test_removing_the_protocol_without_live_clients_never_touches_the_app(
    container: str, api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    state = await imported(world, container=container)
    protocol_id = state.protocol.id
    fake, app = state.scene.fake, state.scene.app
    await api.post(f"/api/v1/credentials/{state.credential.id}/revoke")
    await drain(worker)
    files_before, peers_before = dict(app.files), [p.public_key for p in app.peers]
    fake.commands.clear()
    fake.writes.clear()
    fake.copies.clear()

    response = await api.delete(f"/api/v1/servers/{state.scene.server.id}/protocol")
    assert response.status_code == 202
    assert await drain(worker) == 1
    job = await world.session.get(Job, UUID(response.json()["id"]))
    assert job is not None
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS, job.error

    # Панель могла бы убирать лишь СВОЙ контейнер amnezia-awg (его нет); файлы и контейнер приложения не трогаются.
    touching_the_app = [
        c for c in fake.changing_commands() if re.search(rf"{container}(?![\w-])", c) or "/opt/amnezia" in c
    ]
    assert touching_the_app == [] and fake.writes == [] and fake.copies == []
    assert app.files == files_before and [p.public_key for p in app.peers] == peers_before
    assert fake.containers == {container: "running"}  # контейнер приложения не остановлен и не удалён
    assert await ProtocolRepository(world.session).get_by_id(protocol_id) is not None  # запись приложения цела


async def test_installing_the_panel_protocol_next_to_a_v1_app_fails_and_keeps_imported_clients(
    api: httpx.AsyncClient, world: World, worker: JobWorker
) -> None:
    state = await imported(world, container="amnezia-awg")
    fake, app = state.scene.fake, state.scene.app
    files_before = dict(app.files)

    response = await api.post(f"/api/v1/servers/{state.scene.server.id}/protocol")
    assert response.status_code == 202
    await drain(worker)
    job = await world.session.get(Job, UUID(response.json()["id"]))
    assert job is not None
    await world.session.refresh(job)
    assert job.status == JobStatus.FAILED and "AmneziaVPN" in (job.error or "")
    assert fake.changing_commands() == [] and app.files == files_before
    await world.session.refresh(state.credential)
    assert state.credential.revoked_at is None
    types = {p.type for p in await ProtocolRepository(world.session).list_by_server_id(state.scene.server.id)}
    assert types == {ProtocolType.AMNEZIA_APP_AWG}
