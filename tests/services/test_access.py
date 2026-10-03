"""Подписки, гранты и клиенты на сервере: переходы статусов, истечение, отзыв, гонки, запреты, аудит."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from core.exceptions import AlreadyExistsError, InactiveAccessError, LiveCredentialExistsError, NotFoundError
from core.security import decrypt_secret
from infrastructure.vpn.awg_config import list_peers
from modules.access.models import AccessGrantStatus as S
from modules.access.schemas import AccessGrantCreate, SubscriptionCreate, SubscriptionUpdate
from modules.access.service import AccessGrantService, SubscriptionService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import JobType
from modules.vpn_users.service import VPNUserService
from tests.fakes.fake_ssh import CONFIG_FILE
from tests.services.conftest import World

HOUR = timedelta(hours=1)


def subscriptions(world: World) -> SubscriptionService:
    return SubscriptionService(world.session, SYSTEM_ACTOR)


def grants(world: World) -> AccessGrantService:
    return AccessGrantService(world.session, SYSTEM_ACTOR)


# --- подписки ------------------------------------------------------------------------------------------------------


async def test_subscription_requires_user_and_one_active(world: World) -> None:
    now = datetime.now(timezone.utc)
    period = SubscriptionCreate(valid_from=now, valid_until=now + HOUR)
    with pytest.raises(NotFoundError):
        await subscriptions(world).create(uuid4(), period)
    _, protocol, _ = await world.server()
    user, subscription, _ = await world.access(protocol)
    with pytest.raises(AlreadyExistsError):
        await subscriptions(world).create(user.id, period)
    await subscriptions(world).revoke(subscription.id)
    assert await subscriptions(world).create(user.id, period)  # после отзыва — можно новую


async def test_revoke_cascades_and_is_idempotent(world: World) -> None:
    _, protocol, _ = await world.server()
    _, subscription, grant = await world.access(protocol)
    revoked = await subscriptions(world).revoke(subscription.id)
    first_revoked_at = revoked.revoked_at
    await world.session.refresh(grant)
    assert revoked.status == S.REVOKED and grant.status == S.REVOKED and grant.revoked_at == first_revoked_at
    again = await subscriptions(world).revoke(subscription.id)
    assert again.revoked_at == first_revoked_at
    assert len(await world.audit(AuditAction.SUBSCRIPTION_REVOKED)) == 1
    (access_revoked,) = await world.audit(AuditAction.ACCESS_REVOKED, grant.id)
    assert access_revoked.meta == {"reason": "subscription_revoked", "subscription_id": str(subscription.id)}


async def test_expire_only_after_valid_until_and_revoked_wins(world: World) -> None:
    _, protocol, _ = await world.server()
    _, current, grant = await world.access(protocol)
    assert (await subscriptions(world).expire(current.id)).status == S.PENDING  # срок ещё не вышел

    await subscriptions(world).update(current.id, SubscriptionUpdate(valid_until=datetime.now(timezone.utc) - HOUR))
    assert (await subscriptions(world).expire(current.id)).status == S.EXPIRED
    await world.session.refresh(grant)
    assert grant.status == S.EXPIRED
    await subscriptions(world).expire(current.id)  # повтор — без новых записей
    assert len(await world.audit(AuditAction.SUBSCRIPTION_EXPIRED)) == 1
    assert [log.admin_id for log in await world.audit(AuditAction.ACCESS_EXPIRED, grant.id)] == [None]

    _, revoked, _ = await world.access(protocol, starts=-2 * HOUR, ends=-HOUR)
    await subscriptions(world).revoke(revoked.id)
    assert (await subscriptions(world).expire(revoked.id)).status == S.REVOKED  # явный отзыв не переписывается


async def test_activate_only_inside_period(world: World) -> None:
    _, protocol, _ = await world.server()
    _, future, _ = await world.access(protocol, starts=HOUR, ends=2 * HOUR)
    assert (await subscriptions(world).activate(future.id)).status == S.PENDING
    _, current, _ = await world.access(protocol)
    assert (await subscriptions(world).activate(current.id)).status == S.ACTIVE
    await subscriptions(world).activate(current.id)
    assert len(await world.audit(AuditAction.SUBSCRIPTION_ACTIVATED, current.id)) == 1
    _, past, _ = await world.access(protocol, starts=-2 * HOUR, ends=-HOUR)
    assert (await subscriptions(world).activate(past.id)).status == S.PENDING  # не активируем уже истёкшую


async def test_due_lists_for_scheduler(world: World) -> None:
    _, protocol, _ = await world.server()
    _, current, _ = await world.access(protocol)
    _, future, _ = await world.access(protocol, starts=HOUR, ends=2 * HOUR)
    _, past, _ = await world.access(protocol, starts=-2 * HOUR, ends=-HOUR)
    assert set(await subscriptions(world).list_due_for_expiry()) == {past.id}
    assert set(await subscriptions(world).list_due_for_activation()) == {current.id}
    assert future.id not in await subscriptions(world).list_due_for_activation()


# --- гранты --------------------------------------------------------------------------------------------------------


async def test_grant_derives_server_and_rejects_conflict(world: World) -> None:
    server, protocol, _ = await world.server()
    _, subscription, grant = await world.access(protocol)
    assert grant.server_id == server.id
    with pytest.raises(AlreadyExistsError):
        await grants(world).create(subscription.id, AccessGrantCreate(protocol_id=protocol.id))
    with pytest.raises(NotFoundError):
        await grants(world).create(subscription.id, AccessGrantCreate(protocol_id=uuid4()))


async def test_grant_revoke_is_idempotent(world: World) -> None:
    _, protocol, _ = await world.server()
    _, _, grant = await world.access(protocol)
    first = (await grants(world).revoke(grant.id)).revoked_at
    assert (await grants(world).revoke(grant.id)).revoked_at == first
    assert await world.jobs(JobType.REVOKE_ACCESS) == []  # клиента на сервере не было — отзывать нечего


# --- клиенты на сервере (credential) -------------------------------------------------------------------------------


async def test_issue_respects_period(world: World) -> None:
    _, protocol, _ = await world.server()
    _, _, future = await world.access(protocol, starts=HOUR)
    with pytest.raises(InactiveAccessError, match="not started"):
        await world.credentials().schedule_issue(future.id)
    _, _, past = await world.access(protocol, starts=-2 * HOUR, ends=-HOUR)
    with pytest.raises(InactiveAccessError, match="ended"):
        await world.credentials().schedule_issue(past.id)


async def test_schedule_issue_once(world: World) -> None:
    _, protocol, _ = await world.server()
    _, _, grant = await world.access(protocol)
    job = await world.credentials().schedule_issue(grant.id)
    assert (job.type, job.access_grant_id) == (JobType.CREATE_USER, grant.id)
    with pytest.raises(AlreadyExistsError, match="being issued"):
        await world.credentials().schedule_issue(grant.id)


async def test_provision_creates_peer_and_encrypted_credential(world: World) -> None:
    _, protocol, fake = await world.server()
    _, _, grant, credential = await world.issued(protocol)
    assert credential.client_public_key in fake.interface_peers
    assert "PrivateKey" not in credential.encrypted_config  # в БД — только шифротекст
    assert "PrivateKey" in decrypt_secret(credential.encrypted_config)
    await world.session.refresh(grant)
    assert grant.status == S.ACTIVE
    assert len(await world.audit(AuditAction.CREDENTIAL_ISSUED, credential.id)) == 1
    with pytest.raises(AlreadyExistsError, match="live credential"):
        await world.credentials().schedule_issue(grant.id)


async def test_provision_is_idempotent(world: World) -> None:
    _, protocol, fake = await world.server()
    _, _, grant, credential = await world.issued(protocol)
    again = await world.credentials().provision(grant.id)
    assert again.id == credential.id
    assert len(list_peers(fake.files[CONFIG_FILE])) == 1  # второго клиента не появилось


async def test_reveal_config_is_audited_and_blocked_after_revoke(world: World) -> None:
    _, protocol, _ = await world.server()
    _, _, grant, credential = await world.issued(protocol)
    revealed = await world.credentials().reveal_config(credential.id)
    assert "[Interface]" in revealed.config
    (viewed,) = await world.audit(AuditAction.CONFIG_VIEWED, credential.id)
    assert "[Interface]" not in str(viewed.meta)  # в журнале — факт выдачи, а не конфиг
    await world.credentials().deprovision(grant.id)
    with pytest.raises(InactiveAccessError):
        await world.credentials().reveal_config(credential.id)


async def test_deprovision_removes_peer_and_is_idempotent(world: World) -> None:
    _, protocol, fake = await world.server()
    _, _, grant, credential = await world.issued(protocol)
    revoked = await world.credentials().deprovision(grant.id)
    assert revoked is not None and revoked.revoked_at is not None
    assert credential.client_public_key not in fake.interface_peers
    assert await world.credentials().deprovision(grant.id) is None


async def test_rotation_after_credential_revoke(world: World) -> None:
    _, protocol, _ = await world.server()
    _, _, grant, credential = await world.issued(protocol)
    job = await world.credentials().schedule_revoke(credential.id)
    assert job.type == JobType.REVOKE_ACCESS
    await world.credentials().deprovision(grant.id)
    assert await world.credentials().schedule_revoke(credential.id) == credential  # уже отозван — без задачи
    new = await world.credentials().provision(grant.id)
    assert new.id != credential.id and new.client_public_key != credential.client_public_key


async def test_revoke_with_live_client_schedules_server_revoke(world: World) -> None:
    _, protocol, _ = await world.server()
    _, _, grant, _ = await world.issued(protocol)
    await grants(world).revoke(grant.id)
    assert [job.access_grant_id for job in await world.jobs(JobType.REVOKE_ACCESS)] == [grant.id]


async def test_revoke_while_issue_is_queued(world: World) -> None:
    """Гонка 12.3: отозвали, пока CREATE_USER ждёт в очереди → отзыв всё равно ставится, а выдача откажет."""
    _, protocol, fake = await world.server()
    _, _, grant = await world.access(protocol)
    await world.credentials().schedule_issue(grant.id)
    await grants(world).revoke(grant.id)
    assert len(await world.jobs(JobType.REVOKE_ACCESS)) == 1
    with pytest.raises(InactiveAccessError):
        await world.credentials().provision(grant.id)
    assert list_peers(fake.files[CONFIG_FILE]) == []


async def test_expire_revokes_live_clients(world: World) -> None:
    _, protocol, _ = await world.server()
    _, subscription, grant, _ = await world.issued(protocol)
    await subscriptions(world).update(
        subscription.id, SubscriptionUpdate(valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await subscriptions(world).expire(subscription.id)
    assert [job.access_grant_id for job in await world.jobs(JobType.REVOKE_ACCESS)] == [grant.id]


async def test_cannot_delete_with_live_client(world: World) -> None:
    _, protocol, _ = await world.server()
    user, subscription, grant, _ = await world.issued(protocol)
    with pytest.raises(LiveCredentialExistsError):
        await grants(world).delete(grant.id)
    with pytest.raises(LiveCredentialExistsError):
        await subscriptions(world).delete(subscription.id)
    with pytest.raises(LiveCredentialExistsError):
        await VPNUserService(world.session, SYSTEM_ACTOR).delete(user.id)
    await world.credentials().deprovision(grant.id)
    await VPNUserService(world.session, SYSTEM_ACTOR).delete(user.id)  # после отзыва — можно
