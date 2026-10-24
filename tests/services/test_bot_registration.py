"""Регистрация через бота (этап 25.1): приглашение -> пользователь, подписка, грант, задача CREATE_USER ->
active/failed.

Настоящие Postgres и Redis, SSH фейковый. Воркер - через drain(): хук `on_issue_finished` живёт в нём.
"""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from core.database.postgres import session_factory
from core.exceptions import NotFoundError, RegistrationConflictError
from infrastructure.workers.scheduler import fail_stale_jobs
from infrastructure.workers.worker import JobWorker
from modules.access.models import AccessGrantStatus
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.service import AccessGrantService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction
from modules.bot.models.invite import BotInvite
from modules.bot.models.registration import BotRegistration
from modules.bot.registrations import BotRegistrationService
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.queue import JobQueue
from modules.vpn_users.models.user import VPNUser
from tests.bot_support import (
    EMAIL,
    INVITE_REFUSED,
    NAME,
    RACE_LOSER_ERRORS,
    RETRY_REFUSED,
    SECOND_EMAIL,
    SECOND_TELEGRAM_ID,
    TELEGRAM_ID,
    USERNAME,
    BotScene,
    PendingPayment,
    audit_dump,
    bot_action,
    bot_audit_dump,
    bot_scene,
    create_invite,
    fetch,
    now,
    register,
    registration_service,
    reload,
    snapshot,
    status_value,
)
from tests.fixtures import World
from tests.integration.conftest import drain


@pytest.fixture
def worker() -> JobWorker:
    return JobWorker(JobQueue())


def state(registration: BotRegistration) -> str:
    return status_value(registration.status)


async def user_of(registration: BotRegistration) -> VPNUser:
    user = await fetch(VPNUser, registration.user_id)
    assert user is not None
    return user  # type: ignore[no-any-return]


async def issue_job(world: World) -> Job:
    (job,) = await world.jobs(JobType.CREATE_USER)
    await world.session.refresh(job)
    return job


async def new_invite(world: World, scene: BotScene, **kwargs: Any) -> str:
    _, code = await create_invite(world.session, scene.protocol.id, **kwargs)
    return code


async def failed_registration(world: World, scene: BotScene, worker: JobWorker) -> tuple[BotRegistration, str]:
    """Регистрация, чья выдача упала: SSH-сервер недоступен."""
    scene.fake.reachable = False
    code = await new_invite(world, scene)
    registration = await register(world.session, code, key=scene.key)
    await drain(worker)
    await reload(world.session, registration)
    assert state(registration) == "failed"
    return registration, code


# --- успешный путь -------------------------------------------------------------------------------------------------


async def test_register_creates_the_user_with_telegram_fields(world: World) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    user = await user_of(registration)
    assert (user.username, user.email) == (NAME, EMAIL)
    assert (user.telegram_id, user.telegram_username) == (TELEGRAM_ID, USERNAME)


async def test_register_gives_a_subscription_for_the_invite_days_starting_now(world: World) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene, days=45), key=scene.key)
    async with session_factory() as other:
        (subscription,) = (
            (await other.execute(select(Subscription).where(Subscription.user_id == registration.user_id)))
            .scalars()
            .all()
        )
    assert subscription.valid_until - subscription.valid_from == timedelta(days=45)
    assert timedelta(0) <= now() - subscription.valid_from < timedelta(minutes=1)  # не в будущем: выдача сразу


@pytest.mark.parametrize("days", [1, 3650])
async def test_register_accepts_the_shortest_and_the_longest_invite(world: World, days: int) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene, days=days), key=scene.key)
    async with session_factory() as other:
        subscription = (
            await other.execute(select(Subscription).where(Subscription.user_id == registration.user_id))
        ).scalar_one()
    assert subscription.valid_until - subscription.valid_from == timedelta(days=days)


async def test_register_grants_access_to_the_protocol_of_the_invite(world: World) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    grant = await fetch(AccessGrant, registration.access_grant_id)
    assert (grant.protocol_id, grant.server_id) == (scene.protocol.id, scene.server.id)
    assert grant.status == AccessGrantStatus.PENDING  # ACTIVE станет, когда воркер создаст клиента


async def test_register_queues_one_issue_job_for_the_grant_without_secrets(world: World) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene)
    registration = await register(world.session, code, key=scene.key)
    job = await issue_job(world)
    assert (job.access_grant_id, job.server_id) == (registration.access_grant_id, scene.server.id)
    assert job.status == JobStatus.PENDING
    assert code not in str(job.params) and EMAIL not in str(job.params)
    assert await JobQueue().size() == 1


async def test_register_returns_a_provisioning_registration_that_remembers_its_origin(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    registration = await register(world.session, code, key=scene.key)
    assert state(registration) == "provisioning"
    assert (registration.invite_id, registration.bot_key_id) == (invite.id, scene.key.id)
    assert (registration.telegram_id, registration.name, registration.email) == (TELEGRAM_ID, NAME, EMAIL)
    assert registration.error is None and registration.finished_at is None


async def test_register_takes_the_invite(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    await register(world.session, code, key=scene.key)
    await world.session.refresh(invite)
    assert invite.used_at is not None


async def test_register_goes_through_the_existing_audit_chain(world: World) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    assert len(await world.audit(AuditAction.VPN_USER_CREATED, registration.user_id)) == 1
    assert len(await world.audit(AuditAction.SUBSCRIPTION_CREATED)) == 1
    assert len(await world.audit(AuditAction.ACCESS_GRANTED, registration.access_grant_id)) == 1
    assert len(await world.audit(bot_action("bot_registration_created"), registration.id)) == 1


async def test_register_audit_has_no_code_email_or_telegram_id(world: World) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene)
    await register(world.session, code, key=scene.key)
    dump = await bot_audit_dump()
    assert "bot_registration_created" in dump
    for secret in (code, EMAIL, EMAIL.split("@")[0], str(TELEGRAM_ID)):
        assert secret not in dump
    assert code not in await audit_dump()
    # Весь журнал, не только события бота: вместе с регистрацией пишется vpn_user_created, и в нём почты тоже нет.
    assert EMAIL not in await audit_dump() and str(TELEGRAM_ID) not in await audit_dump()


async def test_register_with_a_short_lived_but_unexpired_invite_works(world: World) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene, expires_in=timedelta(seconds=30))
    assert state(await register(world.session, code, key=scene.key)) == "provisioning"


# --- повтор ---------------------------------------------------------------------------------------------------------


async def test_the_same_telegram_repeating_gets_the_same_registration(world: World) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene)
    first = await register(world.session, code, key=scene.key)
    again = await register(world.session, code, key=scene.key)
    assert again.id == first.id


async def test_a_repeat_creates_nothing_new(world: World) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene)
    await register(world.session, code, key=scene.key)
    before = await snapshot()
    await register(world.session, code, key=scene.key)
    assert await snapshot() == before
    assert len(await world.audit(bot_action("bot_registration_created"))) == 1


async def test_a_repeat_after_the_client_was_issued_returns_the_active_registration(
    world: World, worker: JobWorker
) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene)
    first = await register(world.session, code, key=scene.key)
    await drain(worker)
    again = await register(world.session, code, key=scene.key)
    assert again.id == first.id
    await reload(world.session, again)
    assert state(again) == "active"
    assert len(await world.jobs(JobType.CREATE_USER)) == 1


async def test_a_repeat_after_a_failure_returns_the_failed_registration_and_does_not_retry(
    world: World, worker: JobWorker
) -> None:
    scene = await bot_scene(world)
    failed, code = await failed_registration(world, scene, worker)
    again = await register(world.session, code, key=scene.key)
    assert again.id == failed.id
    await reload(world.session, again)
    assert state(again) == "failed"  # повторяет админ (retry), а не сам бот
    assert len(await world.jobs(JobType.CREATE_USER)) == 1


# --- отказы без побочных эффектов ----------------------------------------------------------------------------------


async def test_another_telegram_cannot_use_a_used_invite(world: World) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene)
    await register(world.session, code, key=scene.key)
    before = await snapshot()
    with pytest.raises(RegistrationConflictError) as refused:
        await register(
            world.session, code, key=scene.key, telegram_id=SECOND_TELEGRAM_ID, email=SECOND_EMAIL, name="Мария"
        )
    assert refused.value.reason == "invite_used"
    assert await snapshot() == before


async def test_another_telegram_cannot_use_the_invite_of_a_failed_registration(world: World, worker: JobWorker) -> None:
    """Приглашение остаётся занятым и при failed: админ повторяет регистрацию, а не выдаёт чужое место."""
    scene = await bot_scene(world)
    _, code = await failed_registration(world, scene, worker)
    with pytest.raises(RegistrationConflictError) as refused:
        await register(world.session, code, key=scene.key, telegram_id=SECOND_TELEGRAM_ID, email=SECOND_EMAIL)
    assert refused.value.reason == "invite_used"


@pytest.mark.parametrize(
    ("stored", "wanted"),
    [
        (EMAIL, EMAIL),
        (EMAIL, EMAIL.upper()),
        ("Ivan.Petrov@Example.org", EMAIL),
    ],
    ids=["same", "upper-case-request", "mixed-case-stored"],
)
async def test_a_taken_email_is_refused_ignoring_case_and_creates_nothing(
    world: World, stored: str, wanted: str
) -> None:
    scene = await bot_scene(world)
    world.session.add(VPNUser(username="old", email=stored))
    await world.session.commit()
    invite, code = await create_invite(world.session, scene.protocol.id)
    before = await snapshot()
    with pytest.raises(RegistrationConflictError) as refused:
        await register(world.session, code, key=scene.key, email=wanted)
    assert refused.value.reason == "email_taken"
    assert await snapshot() == before
    assert (await fetch(BotInvite, invite.id)).used_at is None  # приглашение не потрачено


async def test_a_taken_telegram_id_is_refused_and_creates_nothing(world: World) -> None:
    scene = await bot_scene(world)
    world.session.add(VPNUser(username="old", email="old@example.org", telegram_id=TELEGRAM_ID))
    await world.session.commit()
    invite, code = await create_invite(world.session, scene.protocol.id)
    before = await snapshot()
    with pytest.raises(RegistrationConflictError) as refused:
        await register(world.session, code, key=scene.key)
    assert refused.value.reason == "telegram_taken"
    assert await snapshot() == before
    assert (await fetch(BotInvite, invite.id)).used_at is None


async def test_a_telegram_with_an_unfinished_registration_cannot_start_another(world: World) -> None:
    scene = await bot_scene(world)
    await register(world.session, await new_invite(world, scene), key=scene.key)
    second_invite, second_code = await create_invite(world.session, scene.protocol.id)
    before = await snapshot()
    with pytest.raises(RegistrationConflictError) as refused:
        await register(world.session, second_code, key=scene.key, email=SECOND_EMAIL)
    assert refused.value.reason == "telegram_taken"
    assert await snapshot() == before
    assert (await fetch(BotInvite, second_invite.id)).used_at is None


async def test_an_unknown_invite_is_refused_and_creates_nothing(world: World) -> None:
    scene = await bot_scene(world)
    before = await snapshot()
    with pytest.raises(INVITE_REFUSED):
        await register(world.session, "no-such-code-at-all", key=scene.key)
    assert await snapshot() == before


async def test_an_expired_invite_is_refused_and_creates_nothing(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    invite.expires_at = now() - timedelta(seconds=1)  # граница: срок вышел секунду назад
    await world.session.commit()
    before = await snapshot()
    with pytest.raises(INVITE_REFUSED):
        await register(world.session, code, key=scene.key)
    assert await snapshot() == before


async def test_a_revoked_invite_is_refused_and_creates_nothing(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    invite.revoked_at = now()
    await world.session.commit()
    before = await snapshot()
    with pytest.raises(INVITE_REFUSED):
        await register(world.session, code, key=scene.key)
    assert await snapshot() == before


async def test_an_invite_whose_protocol_was_deleted_is_refused_and_creates_nothing(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    async with session_factory() as other:
        await other.execute(text("DELETE FROM protocols WHERE id = :id"), {"id": scene.protocol.id})
        await other.commit()
    before = await snapshot()
    with pytest.raises(INVITE_REFUSED):
        await register(world.session, code, key=scene.key)
    assert await snapshot() == before
    assert (await fetch(BotInvite, invite.id)).used_at is None


# --- заглушка оплаты -----------------------------------------------------------------------------------------------


async def test_unpaid_registration_waits_for_payment_without_creating_access(world: World) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene)
    registration = await register(world.session, code, key=scene.key, payment=PendingPayment())
    assert state(registration) == "payment_pending"
    snap = await snapshot()
    assert (snap["users"], snap["subscriptions"], snap["grants"], snap["issue_jobs"]) == (0, 0, 0, 0)
    assert registration.user_id is None and registration.access_grant_id is None


async def test_unpaid_registration_still_holds_the_invite(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    await register(world.session, code, key=scene.key, payment=PendingPayment())
    assert (await fetch(BotInvite, invite.id)).used_at is not None
    with pytest.raises(RegistrationConflictError) as refused:
        await register(
            world.session,
            code,
            key=scene.key,
            payment=PendingPayment(),
            telegram_id=SECOND_TELEGRAM_ID,
            email=SECOND_EMAIL,
        )
    assert refused.value.reason == "invite_used"


async def test_unpaid_registration_repeat_returns_the_same_one(world: World) -> None:
    scene = await bot_scene(world)
    code = await new_invite(world, scene)
    first = await register(world.session, code, key=scene.key, payment=PendingPayment())
    again = await register(world.session, code, key=scene.key, payment=PendingPayment())
    assert again.id == first.id and (await snapshot())["registrations"] == 1


# --- гонки (окно открывается в AccessGrantService.stage_create) ---------------------------------------------------


def open_window(monkeypatch: pytest.MonkeyPatch, rival: Any) -> dict[str, asyncio.Task[Any]]:
    """Пока первая регистрация выдаёт грант (приглашение проверено, пользователь и подписка уже во flush, commit ещё
    нет), стартует вторая в другой сессии и успевает дойти до блокировки или уникального индекса. Окно открывается
    один раз."""
    tasks: dict[str, asyncio.Task[Any]] = {}
    original = AccessGrantService.stage_create

    async def stage_create_with_window(self: AccessGrantService, *args: Any, **kwargs: Any) -> Any:
        if "rival" not in tasks:
            tasks["rival"] = asyncio.ensure_future(rival())
            await asyncio.sleep(0.5)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AccessGrantService, "stage_create", stage_create_with_window)
    return tasks


async def outcome(awaitable: Any) -> Any:
    try:
        return await awaitable
    except Exception as exc:  # noqa: BLE001 - важен сам исход гонки
        return exc


async def test_two_telegrams_racing_for_one_invite_only_one_registers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)

    async def rival() -> BotRegistration:
        async with session_factory() as other:
            return await register(other, code, key=scene.key, telegram_id=SECOND_TELEGRAM_ID, email=SECOND_EMAIL)

    tasks = open_window(monkeypatch, rival)
    first = await outcome(register(world.session, code, key=scene.key))
    second = await outcome(tasks["rival"])

    winners = [item for item in (first, second) if isinstance(item, BotRegistration)]
    losers = [item for item in (first, second) if not isinstance(item, BotRegistration)]
    assert len(winners) == 1 and len(losers) == 1
    assert isinstance(losers[0], RegistrationConflictError) and losers[0].reason == "invite_used"
    snap = await snapshot()
    assert (snap["users"], snap["registrations"], snap["grants"], snap["issue_jobs"]) == (1, 1, 1, 1)
    assert (await fetch(BotInvite, invite.id)).used_at is not None


async def test_two_invites_racing_for_one_email_only_one_user_is_created(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = await bot_scene(world)
    first_invite, first_code = await create_invite(world.session, scene.protocol.id)
    second_invite, second_code = await create_invite(world.session, scene.protocol.id)

    async def rival() -> BotRegistration:
        async with session_factory() as other:
            return await register(other, second_code, key=scene.key, telegram_id=SECOND_TELEGRAM_ID)

    tasks = open_window(monkeypatch, rival)
    first = await outcome(register(world.session, first_code, key=scene.key))
    second = await outcome(tasks["rival"])

    results = [first, second]
    assert sum(isinstance(item, BotRegistration) for item in results) == 1
    (loser,) = [item for item in results if not isinstance(item, BotRegistration)]
    assert isinstance(loser, RACE_LOSER_ERRORS)
    snap = await snapshot()
    assert (snap["users"], snap["registrations"], snap["grants"], snap["issue_jobs"]) == (1, 1, 1, 1)
    # у проигравшего приглашение не потрачено: человек может повторить с другой почтой
    (winner,) = [item for item in results if isinstance(item, BotRegistration)]
    spent = {
        invite_id for invite_id in (first_invite.id, second_invite.id) if (await fetch(BotInvite, invite_id)).used_at
    }
    assert spent == {winner.invite_id}


# --- хук воркера ---------------------------------------------------------------------------------------------------


async def test_issued_client_makes_the_registration_active(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    await drain(worker)
    await reload(world.session, registration)
    assert state(registration) == "active"
    assert registration.finished_at is not None and registration.error is None
    assert len(scene.fake.interface_peers) == 1  # клиент настоящий: он на сервере
    grant = await fetch(AccessGrant, registration.access_grant_id)
    assert grant.status == AccessGrantStatus.ACTIVE
    assert len(await world.audit(bot_action("bot_registration_activated"), registration.id)) == 1


async def test_the_invite_stays_used_after_activation(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    await register(world.session, code, key=scene.key)
    await drain(worker)
    await world.session.refresh(invite)
    assert invite.used_at is not None


async def test_failed_issue_makes_the_registration_failed_keeping_user_and_grant(
    world: World, worker: JobWorker
) -> None:
    scene = await bot_scene(world)
    failed, _ = await failed_registration(world, scene, worker)
    assert failed.error
    snap = await snapshot()
    assert (snap["users"], snap["grants"], snap["used_invites"]) == (1, 1, 1)  # админ сможет повторить
    assert len(await world.audit(bot_action("bot_registration_failed"), failed.id)) == 1
    job = await issue_job(world)
    assert job.status == JobStatus.FAILED


async def test_ssh_error_text_with_a_secret_is_redacted_in_the_registration(
    world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def leaking(self: JobWorker, session: Any, access_grant_id: UUID, actor: Any) -> None:
        raise RuntimeError("awg: bad line 'PrivateKey = aGVsbG8gd29ybGQgaGVsbG8gd29ybGQgaGVsbG8=' in config")

    monkeypatch.setattr(JobWorker, "create_user", leaking)
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    await drain(worker)
    await reload(world.session, registration)
    assert state(registration) == "failed" and registration.error
    assert "aGVsbG8" not in registration.error
    assert "aGVsbG8" not in await audit_dump()


async def test_a_non_bot_issue_job_does_not_touch_registrations_or_crash_the_hook(
    world: World, worker: JobWorker
) -> None:
    _, protocol, _ = await world.server()
    _, _, grant = await world.access(protocol)
    job = await world.credentials().schedule_issue(grant.id)
    await drain(worker)
    await world.session.refresh(job)
    assert job.status == JobStatus.SUCCESS
    assert (await snapshot())["registrations"] == 0


@pytest.mark.parametrize("outcome_name", ["success", "failure"])
async def test_a_crashing_hook_rolls_back_the_final_job_status(
    world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch, outcome_name: str
) -> None:
    """Хук в одной транзакции со статусом задачи: упал хук - не зафиксирован ни SUCCESS/FAILED,
    ни статус регистрации."""

    async def broken(self: BotRegistrationService, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("hook is broken")

    monkeypatch.setattr(BotRegistrationService, "on_issue_finished", broken)
    scene = await bot_scene(world)
    scene.fake.reachable = outcome_name == "success"
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    await drain(worker)
    await reload(world.session, registration)
    assert state(registration) == "provisioning"
    assert (await issue_job(world)).status == JobStatus.RUNNING


async def test_stale_job_rule_fails_the_registration_whose_hook_never_ran(
    world: World, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Риск плана: хук упал в ветке сбоя -> задача станет stale, reconcile в правиле stale_jobs закроет регистрацию."""
    original = BotRegistrationService.on_issue_finished

    async def broken(self: BotRegistrationService, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("hook is broken")

    monkeypatch.setattr(BotRegistrationService, "on_issue_finished", broken)
    scene = await bot_scene(world)
    scene.fake.reachable = False
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    await drain(worker)
    monkeypatch.setattr(BotRegistrationService, "on_issue_finished", original)

    job = await issue_job(world)
    job.started_at = now() - timedelta(hours=2)
    await world.session.commit()
    await fail_stale_jobs(1800)

    await reload(world.session, registration)
    assert state(registration) == "failed" and registration.error


# --- reconcile -----------------------------------------------------------------------------------------------------


async def test_reconcile_fails_a_registration_whose_job_failed(world: World) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    job = await issue_job(world)
    job.status, job.error, job.finished_at = JobStatus.FAILED, "Connection error", now()
    await world.session.commit()
    await registration_service(world.session).reconcile()
    await reload(world.session, registration)
    assert state(registration) == "failed" and registration.error


async def test_reconcile_activates_a_registration_whose_client_exists(world: World) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    await world.credentials().provision(registration.access_grant_id)
    job = await issue_job(world)
    job.status, job.finished_at = JobStatus.SUCCESS, now()
    await world.session.commit()
    await registration_service(world.session).reconcile()
    await reload(world.session, registration)
    assert state(registration) == "active" and registration.finished_at is not None


@pytest.mark.parametrize("job_status", [JobStatus.PENDING, JobStatus.RUNNING])
async def test_reconcile_leaves_a_registration_whose_job_is_still_in_flight(
    world: World, job_status: JobStatus
) -> None:
    scene = await bot_scene(world)
    registration = await register(world.session, await new_invite(world, scene), key=scene.key)
    job = await issue_job(world)
    job.status = job_status
    await world.session.commit()
    await registration_service(world.session).reconcile()
    await reload(world.session, registration)
    assert state(registration) == "provisioning"


async def test_reconcile_is_idempotent_and_leaves_finished_registrations_alone(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    active = await register(world.session, await new_invite(world, scene), key=scene.key)
    await drain(worker)
    service = registration_service(world.session)
    await service.reconcile()
    await service.reconcile()
    await reload(world.session, active)
    assert state(active) == "active"
    assert len(await world.audit(bot_action("bot_registration_activated"), active.id)) == 1


async def test_reconcile_does_not_downgrade_a_failed_registration_when_nothing_changed(
    world: World, worker: JobWorker
) -> None:
    scene = await bot_scene(world)
    failed, _ = await failed_registration(world, scene, worker)
    await registration_service(world.session).reconcile()
    await reload(world.session, failed)
    assert state(failed) == "failed"
    assert len(await world.audit(bot_action("bot_registration_failed"), failed.id)) == 1


# --- retry ---------------------------------------------------------------------------------------------------------


async def test_retry_of_a_failed_registration_issues_the_client(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    failed, _ = await failed_registration(world, scene, worker)
    scene.fake.reachable = True
    retried = await registration_service(world.session).retry(failed.id)
    assert state(retried) == "provisioning"
    await drain(worker)
    await reload(world.session, failed)
    assert state(failed) == "active" and failed.error is None
    assert len(scene.fake.interface_peers) == 1
    assert len(await world.audit(bot_action("bot_registration_retried"), failed.id)) == 1
    assert len(await world.jobs(JobType.CREATE_USER)) == 2  # первая упала, вторая выдала клиента


async def test_retry_reuses_the_user_and_grant(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    failed, _ = await failed_registration(world, scene, worker)
    user_id, grant_id = failed.user_id, failed.access_grant_id
    scene.fake.reachable = True
    await registration_service(world.session).retry(failed.id)
    await drain(worker)
    await reload(world.session, failed)
    assert (failed.user_id, failed.access_grant_id) == (user_id, grant_id)
    snap = await snapshot()
    assert (snap["users"], snap["subscriptions"], snap["grants"]) == (1, 1, 1)


async def test_a_second_retry_while_provisioning_is_refused(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    failed, _ = await failed_registration(world, scene, worker)
    scene.fake.reachable = True
    service = registration_service(world.session)
    await service.retry(failed.id)
    with pytest.raises(RETRY_REFUSED):
        await service.retry(failed.id)
    assert len(await world.jobs(JobType.CREATE_USER)) == 2  # третьей задачи нет


async def test_retry_of_an_active_registration_is_refused(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    active = await register(world.session, await new_invite(world, scene), key=scene.key)
    await drain(worker)
    with pytest.raises(RETRY_REFUSED):
        await registration_service(world.session).retry(active.id)
    assert len(await world.jobs(JobType.CREATE_USER)) == 1


async def test_retry_after_the_grant_was_revoked_is_refused(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    failed, _ = await failed_registration(world, scene, worker)
    await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(failed.access_grant_id)
    scene.fake.reachable = True
    with pytest.raises(RETRY_REFUSED):
        await registration_service(world.session).retry(failed.id)
    await reload(world.session, failed)
    assert state(failed) == "failed"
    assert len(await world.jobs(JobType.CREATE_USER)) == 1


async def test_retry_of_an_unknown_registration_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await registration_service(world.session).retry(uuid4())


async def test_retry_that_fails_again_returns_to_failed(world: World, worker: JobWorker) -> None:
    scene = await bot_scene(world)
    failed, _ = await failed_registration(world, scene, worker)
    await registration_service(world.session).retry(failed.id)  # сервер всё ещё недоступен
    await drain(worker)
    await reload(world.session, failed)
    assert state(failed) == "failed" and failed.error


async def test_a_stale_failed_job_does_not_fail_a_registration_retried_meanwhile(
    world: World, worker: JobWorker
) -> None:
    """Старую (зависшую) задачу закрыли, админ повторил выдачу; когда старая наконец дошла до хука, итог за новой."""
    scene = await bot_scene(world)
    failed, _ = await failed_registration(world, scene, worker)
    (old_job,) = await world.jobs(JobType.CREATE_USER)
    await registration_service(world.session).retry(failed.id)  # вторая задача ждёт в очереди
    await world.session.refresh(old_job)
    old_job.status, old_job.error = JobStatus.FAILED, "late failure of the first job"
    await registration_service(world.session).on_issue_finished(old_job)
    await world.session.commit()
    await reload(world.session, failed)
    assert state(failed) == "provisioning"  # ждём итог второй задачи
    await drain(worker)  # сервер всё ещё недоступен
    await reload(world.session, failed)
    assert state(failed) == "failed"
