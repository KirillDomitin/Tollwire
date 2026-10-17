"""Этап 23.3 целиком через сервисы и воркер: отзыв доступа, чей credential относится к установке приложения AmneziaVPN
(`AMNEZIA_APP_AWG2`). Строки протокола/credential создаются в БД вручную — панель сама их для приложения не выдаёт."""

import json
from dataclasses import dataclass
from uuid import UUID

import pytest

from core.security import encrypt_secret
from infrastructure.vpn.awg_config import generate_secret_keys, list_peers
from infrastructure.workers.worker import JobWorker
from modules.access.models import AccessGrantStatus
from modules.access.models.vpn_credential import VPNCredential
from modules.access.service import AccessGrantService, SubscriptionService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import JobStatus, JobType
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from tests.fakes.fake_ssh import APP_CLIENTS_TABLE, AmneziaApp, FakeServer
from tests.fixtures import World
from tests.integration.conftest import drain

CONTAINER = "amnezia-awg2"


@dataclass
class AppGrant:
    fake: FakeServer
    app: AmneziaApp
    server_id: UUID
    grant_id: UUID
    credential: VPNCredential
    key: str  # peer этого credential
    other: str  # чужой peer приложения: после отзыва должен остаться как был
    config_before: str


async def app_grant(world: World) -> AppGrant:
    server, _, fake = await world.server(install=False)
    key, other = generate_secret_keys().public_key, generate_secret_keys().public_key
    app = fake.add_amnezia_app(CONTAINER, peers=[key, other], clients_table={key: "Phone", other: "Other"})
    protocol = VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_APP_AWG2, port=app.port)
    world.session.add(protocol)
    await world.session.commit()
    _, _, grant = await world.access(protocol)
    credential = VPNCredential(
        access_grant_id=grant.id,
        encrypted_config=encrypt_secret("[Interface]\nPrivateKey = client-side-secret\n"),
        client_address="10.8.1.2",
        client_public_key=key,
    )
    world.session.add(credential)
    grant.status = AccessGrantStatus.ACTIVE
    await world.session.commit()
    return AppGrant(fake, app, server.id, grant.id, credential, key, other, app.files[app.config_path])


async def test_revoke_access_removes_the_peer_from_the_app_and_revokes_the_credential(
    world: World, worker: JobWorker
) -> None:
    state = await app_grant(world)
    other_block = state.config_before.split("[Peer]")[2]  # блок чужого peer'а — до символа прежний
    secrets = list(state.app.secrets)

    job = await world.credentials().schedule_revoke(state.credential.id)
    assert job.type == JobType.REVOKE_ACCESS
    assert await drain(worker) == 1

    await world.session.refresh(job)
    await world.session.refresh(state.credential)
    assert job.status == JobStatus.SUCCESS
    assert state.credential.revoked_at is not None
    assert [peer.public_key for peer in list_peers(state.app.files[state.app.config_path])] == [state.other]
    assert other_block in state.app.files[state.app.config_path]
    assert [p.public_key for p in state.app.peers] == [state.other]  # и в живом интерфейсе
    assert [e["clientId"] for e in json.loads(state.app.files[APP_CLIENTS_TABLE])] == [state.other]

    (audit,) = await world.audit(AuditAction.CREDENTIAL_REVOKED, state.credential.id)
    assert audit.meta is not None and audit.meta["public_key"] == state.key
    assert not any(secret in str(audit.meta) for secret in secrets)
    assert await world.audit(AuditAction.CREDENTIAL_REVOKE_FAILED) == []

    assert state.fake.stage_files() == []  # промежуточных файлов с приватным ключом сервера не осталось
    assert state.fake.unexpected_changing_commands() == []


async def test_revoke_access_when_the_peer_is_already_gone_succeeds_without_touching_the_server(
    world: World, worker: JobWorker
) -> None:
    """Пользователь сам удалил клиента в приложении: отзыв идемпотентен, credential закрывается, сервер не меняется."""
    state = await app_grant(world)
    state.app.files[state.app.config_path] = state.app.files[state.app.config_path].replace(state.key, "x" * 43 + "=")
    state.app.files[APP_CLIENTS_TABLE] = "[]"
    state.fake.commands.clear()
    changed_config = state.app.files[state.app.config_path]

    await world.credentials().schedule_revoke(state.credential.id)
    assert await drain(worker) == 1

    await world.session.refresh(state.credential)
    assert state.credential.revoked_at is not None
    assert state.app.files[state.app.config_path] == changed_config and state.app.applies <= 1  # только выравнивание
    assert len(state.fake.changing_commands()) <= 1 and state.fake.writes == [] and state.fake.copies == []
    assert len(await world.audit(AuditAction.CREDENTIAL_REVOKED, state.credential.id)) == 1


async def test_failed_apply_keeps_the_credential_live_restores_the_config_and_a_retry_succeeds(
    world: World, worker: JobWorker
) -> None:
    state = await app_grant(world)
    state.app.apply_fails = True
    secrets = list(state.app.secrets)

    job = await world.credentials().schedule_revoke(state.credential.id)
    assert await drain(worker) == 1

    await world.session.refresh(job)
    await world.session.refresh(state.credential)
    assert job.status == JobStatus.FAILED
    assert state.credential.revoked_at is None  # credential остаётся живым: сервер не изменён
    assert state.app.files[state.app.config_path] == state.config_before  # откат
    assert [p.public_key for p in state.app.peers] == [state.key, state.other]
    assert await world.audit(AuditAction.CREDENTIAL_REVOKED) == []
    assert len(await world.audit(AuditAction.CREDENTIAL_REVOKE_FAILED)) == 1
    text = (job.error or "") + str(job.result)
    assert not any(secret in text for secret in secrets) and "PrivateKey" not in text and "PresharedKey" not in text
    assert state.fake.stage_files() == []

    state.app.apply_fails = False
    await world.credentials().schedule_revoke(state.credential.id)
    assert await drain(worker) == 1
    await world.session.refresh(state.credential)
    assert state.credential.revoked_at is not None
    assert [peer.public_key for peer in list_peers(state.app.files[state.app.config_path])] == [state.other]
    assert state.fake.stage_files() == []


async def test_revoke_access_on_a_stopped_app_container_fails_and_keeps_the_credential(
    world: World, worker: JobWorker
) -> None:
    state = await app_grant(world)
    state.fake.containers[CONTAINER] = "exited"

    job = await world.credentials().schedule_revoke(state.credential.id)
    assert await drain(worker) == 1

    await world.session.refresh(job)
    await world.session.refresh(state.credential)
    assert job.status == JobStatus.FAILED and state.credential.revoked_at is None
    assert state.app.files[state.app.config_path] == state.config_before
    assert state.fake.containers[CONTAINER] == "exited"  # контейнер приложения панель не запускает
    assert any(c.startswith("docker ps") for c in state.fake.commands)  # состояние контейнера действительно проверили
    assert not any(c.startswith(("docker exec", "docker start")) for c in state.fake.commands)
    assert state.fake.writes == [] and state.fake.copies == []


@pytest.mark.parametrize("reason", ["grant", "subscription"])
async def test_revoking_the_grant_or_subscription_removes_the_app_peer_via_the_worker(
    world: World, worker: JobWorker, reason: str
) -> None:
    state = await app_grant(world)
    if reason == "grant":
        await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(state.grant_id)
    else:
        grant = await AccessGrantService(world.session, SYSTEM_ACTOR).get(state.grant_id)
        await SubscriptionService(world.session, SYSTEM_ACTOR).revoke(grant.subscription_id)
    assert await drain(worker) == 1

    await world.session.refresh(state.credential)
    assert state.credential.revoked_at is not None
    assert state.key not in state.app.files[state.app.config_path]
    assert state.other in state.app.files[state.app.config_path]
