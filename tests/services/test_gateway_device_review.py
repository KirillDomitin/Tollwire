"""Regression proof for device review: stale state, queued targets and legacy config access."""

from uuid import UUID

import pytest

from core.database.postgres import session_factory
from core.exceptions import NotFoundError, ReasonConflictError
from infrastructure.workers.worker import JobWorker
from modules.access.models import CredentialState
from modules.access.service import VPNCredentialService
from modules.audit.actor import SYSTEM_ACTOR
from modules.bot.models.registration import RegistrationStatus
from modules.bot.registrations import BotRegistrationService
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import KEY, JobQueue
from modules.servers.sync import ServerSyncService
from tests.bot_support import TELEGRAM_ID, create_invite, issue_link, open_link, register
from tests.fixtures import World
from tests.services.test_gateway_devices import gateway, occupied, target


async def process_only(job: Job) -> None:
    queue = JobQueue()
    await queue.client.delete(KEY)
    await queue.enqueue(job.id)
    assert await JobWorker(queue).process_next()


async def test_provision_refreshes_stale_reserved_target_before_any_ssh(world: World) -> None:
    protocol, fake = await gateway(world)
    _, _, grant = await world.access(protocol)
    credential_id = target(await world.credentials().schedule_issue(grant.id))
    stale = await world.credentials().get(credential_id)
    assert stale.state == CredentialState.RESERVED
    async with session_factory() as other:
        await VPNCredentialService(other, SYSTEM_ACTOR).schedule_revoke(credential_id)
    result = await world.credentials().provision(grant.id, credential_id=credential_id)
    assert result.state == CredentialState.REVOKING
    assert fake.interface_peers == set()


async def test_queued_targeted_legacy_revoke_does_not_revoke_replacement(world: World) -> None:
    _, protocol, fake = await world.server()
    assert protocol is not None
    _, _, grant, first = await world.issued(protocol)
    old_revoke = await world.credentials().schedule_revoke(first.id)
    assert isinstance(old_revoke, Job) and old_revoke.type == JobType.REVOKE_ACCESS
    await world.credentials().deprovision(grant.id, credential_id=first.id)
    issue = await world.credentials().schedule_issue(grant.id)
    assert issue.params is not None
    replacement = await world.credentials().provision(grant.id, credential_id=UUID(issue.params["credential_id"]))
    await process_only(old_revoke)
    await world.session.refresh(replacement)
    await world.session.refresh(old_revoke)
    assert old_revoke.status == JobStatus.SUCCESS
    assert replacement.state == CredentialState.ACTIVE and replacement.revoked_at is None
    assert fake.interface_peers == {replacement.client_public_key}


async def test_queued_targeted_legacy_create_does_not_replace_sync_closed_target(world: World) -> None:
    _, protocol, fake = await world.server()
    assert protocol is not None
    _, _, grant = await world.access(protocol)
    issue = await world.credentials().schedule_issue(grant.id)
    assert issue.type == JobType.CREATE_USER and issue.params is not None
    credential_id = UUID(issue.params["credential_id"])
    await world.credentials().schedule_revoke(credential_id)
    await ServerSyncService(world.session).sync(protocol.server_id)
    closed = await world.credentials().get(credential_id)
    assert closed.revoked_at is not None
    await process_only(issue)
    await world.session.refresh(issue)
    assert issue.status == JobStatus.SUCCESS
    assert await occupied(world) == []
    assert fake.interface_peers == set()


async def test_success_hook_does_not_activate_revoked_target_even_with_another_ready_device(world: World) -> None:
    protocol, _ = await gateway(world)
    _, code = await create_invite(world.session, protocol.id)
    registration = await register(world.session, code)
    grant_id = registration.access_grant_id
    assert grant_id is not None
    (original_job,) = await world.jobs(JobType.CREATE_DEVICE)
    first = target(original_job)
    await world.credentials().provision(grant_id, credential_id=first)
    await world.credentials().deprovision(grant_id, credential_id=first)
    replacement_job = await world.credentials().schedule_issue(grant_id)
    await world.credentials().provision(grant_id, credential_id=target(replacement_job))
    original_job.status = JobStatus.SUCCESS
    await BotRegistrationService(world.session, SYSTEM_ACTOR).on_issue_finished(original_job)
    await world.session.commit()
    assert registration.status == RegistrationStatus.FAILED


async def test_legacy_link_is_inaccessible_as_soon_as_revoke_is_scheduled(world: World) -> None:
    protocol, _ = await gateway(world)
    user, _, grant = await world.access(protocol)
    user.telegram_id = TELEGRAM_ID
    await world.session.commit()
    issue = await world.credentials().schedule_issue(grant.id)
    await world.credentials().provision(grant.id, credential_id=target(issue))
    issue.status = JobStatus.SUCCESS
    await world.session.commit()
    link = await issue_link(world.session)
    await world.credentials().schedule_revoke(target(issue))
    with pytest.raises(NotFoundError):
        await open_link(world.session, link.token)
    with pytest.raises(ReasonConflictError) as refusal:
        await issue_link(world.session)
    assert refusal.value.reason == "no_access"


async def test_legacy_config_request_reports_gateway_pending_as_not_ready(world: World) -> None:
    protocol, _ = await gateway(world)
    user, _, grant = await world.access(protocol)
    user.telegram_id = TELEGRAM_ID
    await world.session.commit()
    await world.credentials().schedule_issue(grant.id)
    with pytest.raises(ReasonConflictError) as refusal:
        await issue_link(world.session)
    assert refusal.value.reason == "not_ready"
