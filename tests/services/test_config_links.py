"""Ссылка на страницу конфига (этап 25.3): выпуск `ConfigLinkService.issue_for_telegram` и открытие `open`.

Настоящие Postgres и Redis, SSH фейковый. Ссылка действует, пока жив доступ: это проверяется при каждом открытии
(планировщик и хуки не нужны), поэтому отказы здесь проверяются БЕЗ воркера и планировщика.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text

from core.database.postgres import session_factory
from core.exceptions import NotFoundError
from core.security import decrypt_secret, hash_token
from infrastructure.workers.worker import JobWorker
from modules.access.models import CredentialSource
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.schemas import AccessGrantCreate
from modules.access.service import AccessGrantService, SubscriptionService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.jobs.models.job import JobType
from modules.jobs.queue import JobQueue
from modules.vpn_users.models.user import VPNUser
from tests.bot_support import (
    CONFIG_UNAVAILABLE,
    NO_ACCESS,
    NOT_READY,
    SECOND_TELEGRAM_ID,
    TELEGRAM_ID,
    TOKEN_SHAPE,
    activate_registration,
    audit_dump,
    bot_scene,
    issue_link,
    links_in_db,
    live_links_in_db,
    open_link,
)
from tests.fixtures import World

NEW_AUDIT = "config_link_created"


@pytest.fixture
def worker() -> JobWorker:
    return JobWorker(JobQueue())


async def issued_user(
    world: World, telegram_id: int = TELEGRAM_ID
) -> tuple[VPNUser, Subscription, AccessGrant, VPNCredential]:
    """Человек с живым клиентом, выданным из панели (не ботом), и привязанным Telegram."""
    _, protocol, _ = await world.server()
    assert protocol is not None
    user, subscription, grant, credential = await world.issued(protocol)
    user.telegram_id = telegram_id
    await world.session.commit()
    return user, subscription, grant, credential


async def another_grant(world: World, subscription_id: UUID) -> AccessGrant:
    """Ещё один доступ той же подписки - на другом сервере (на одного человека одна активная подписка)."""
    _, protocol, _ = await world.server()
    assert protocol is not None
    return await AccessGrantService(world.session, SYSTEM_ACTOR).create(
        subscription_id, AccessGrantCreate(protocol_id=protocol.id)
    )


async def second_credential(world: World, subscription_id: UUID) -> VPNCredential:
    """Ещё один живой клиент того же человека: доступ на другом сервере и выдача."""
    grant = await another_grant(world, subscription_id)
    return await world.credentials().provision(grant.id)


async def refusal(coro: Any) -> str:
    from core.exceptions import ReasonConflictError

    with pytest.raises(ReasonConflictError) as caught:
        await coro
    return str(caught.value.reason)


async def assert_nothing_issued() -> None:
    assert await links_in_db() == []
    async with session_factory() as session:
        events = (
            (await session.execute(select(AuditLog).where(AuditLog.action == AuditAction(NEW_AUDIT)))).scalars().all()
        )
    assert events == [] and NEW_AUDIT not in await audit_dump()


async def assert_not_found(world: World, token: str) -> None:
    with pytest.raises(NotFoundError):
        await open_link(world.session, token)


# --- выпуск: что лежит в БД ----------------------------------------------------------------------------------------


async def test_the_token_has_the_agreed_shape(world: World) -> None:
    await issued_user(world)
    issued = await issue_link(world.session)
    assert TOKEN_SHAPE.fullmatch(issued.token)


async def test_the_database_keeps_only_the_hash_of_the_token(world: World) -> None:
    _, _, _, credential = await issued_user(world)
    issued = await issue_link(world.session)

    (row,) = await links_in_db()
    assert row.token_hash == hash_token(issued.token) and row.credential_id == credential.id
    assert row.revoked_at is None and row.last_opened_at is None and row.created_at is not None
    async with session_factory() as session:
        dump = str((await session.execute(text("SELECT * FROM config_links"))).all())
    assert issued.token not in dump


async def test_two_issues_give_two_different_tokens(world: World) -> None:
    await issued_user(world)
    first = await issue_link(world.session)
    second = await issue_link(world.session)
    assert first.token != second.token


# --- выпуск: замена старой ссылки ----------------------------------------------------------------------------------


async def test_a_second_issue_revokes_the_first_and_leaves_one_live_link(world: World) -> None:
    await issued_user(world)
    first = await issue_link(world.session)
    second = await issue_link(world.session)

    rows = await links_in_db()
    assert len(rows) == 2 and len(await live_links_in_db()) == 1
    (live,) = await live_links_in_db()
    assert live.token_hash == hash_token(second.token)
    revoked = next(row for row in rows if row.token_hash == hash_token(first.token))
    assert revoked.revoked_at is not None


async def test_the_replaced_link_opens_no_more_and_the_new_one_does(world: World) -> None:
    await issued_user(world)
    first = await issue_link(world.session)
    second = await issue_link(world.session)

    await assert_not_found(world, first.token)
    assert (await open_link(world.session, second.token)).config


async def test_the_link_of_one_person_is_not_touched_by_the_link_of_another(world: World) -> None:
    await issued_user(world, TELEGRAM_ID)
    mine = await issue_link(world.session, TELEGRAM_ID)
    await issued_user(world, SECOND_TELEGRAM_ID)
    await issue_link(world.session, SECOND_TELEGRAM_ID)

    assert len(await live_links_in_db()) == 2
    assert (await open_link(world.session, mine.token)).config


async def test_a_competing_live_link_that_appears_while_issuing_is_replaced_not_duplicated(world: World) -> None:
    """Гонка: другая транзакция держит клиента (FOR UPDATE) и пока выпуск ждёт, добавляет свою живую ссылку.
    Итог не зависит от того, кто пришёл первым: живая ссылка одна - та, что выпущена позже."""
    from modules.bot.models.config_link import ConfigLink

    _, _, _, credential = await issued_user(world)
    rival_token = "R" * 43
    async with session_factory() as rival:
        await rival.execute(text("SELECT id FROM vpn_credentials WHERE id = :id FOR UPDATE"), {"id": credential.id})
        issuing = asyncio.create_task(issue_link(world.session))
        await asyncio.sleep(0.3)  # выпуск дошёл до блокировки клиента (или ещё нет - итог тот же)
        rival.add(ConfigLink(token_hash=hash_token(rival_token), credential_id=credential.id))
        await rival.commit()
    issued = await asyncio.wait_for(issuing, timeout=10)

    (live,) = await live_links_in_db()
    assert live.token_hash == hash_token(issued.token)
    await assert_not_found(world, rival_token)


# --- выпуск: выбор клиента -----------------------------------------------------------------------------------------


async def test_the_link_points_at_the_client_of_the_bot_registration_even_if_a_newer_one_exists(
    world: World, worker: JobWorker
) -> None:
    scene = await bot_scene(world)
    registration = await activate_registration(world, scene, worker)
    grant = await world.session.get(AccessGrant, registration.access_grant_id)
    assert grant is not None
    await second_credential(world, grant.subscription_id)  # новее, но не от бот-регистрации

    await issue_link(world.session)

    (row,) = await live_links_in_db()
    registration_credential = (
        await world.session.execute(
            text("SELECT id FROM vpn_credentials WHERE access_grant_id = :g"), {"g": registration.access_grant_id}
        )
    ).scalar_one()
    assert row.credential_id == registration_credential


async def test_without_a_bot_registration_the_newest_issued_client_is_chosen(world: World) -> None:
    _, subscription, _, first = await issued_user(world)
    newest = await second_credential(world, subscription.id)

    await issue_link(world.session)

    (row,) = await live_links_in_db()
    assert row.credential_id == newest.id != first.id


async def test_an_imported_client_is_skipped_when_an_issued_one_exists(world: World) -> None:
    _, subscription, _, issued = await issued_user(world)
    other_grant = await another_grant(world, subscription.id)
    world.session.add(
        VPNCredential(
            access_grant_id=other_grant.id,
            encrypted_config=None,
            client_address="10.8.1.50/32",
            client_public_key="imported-public-key",
            source=CredentialSource.IMPORTED,
        )
    )  # новее выданного, но приватного ключа у панели нет
    await world.session.commit()

    await issue_link(world.session)

    (row,) = await live_links_in_db()
    assert row.credential_id == issued.id


async def test_a_person_with_only_imported_clients_gets_config_unavailable(world: World) -> None:
    user, _, grant, issued = await issued_user(world)
    issued.source = CredentialSource.IMPORTED
    issued.encrypted_config = None
    await world.session.commit()

    assert await refusal(issue_link(world.session)) == CONFIG_UNAVAILABLE
    await assert_nothing_issued()


async def test_a_client_still_being_issued_is_not_ready(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    user, _, grant = await world.access(protocol)
    user.telegram_id = TELEGRAM_ID
    await world.session.commit()
    await world.credentials().schedule_issue(grant.id)  # CREATE_USER в очереди, воркер ещё не выдал клиента

    assert await refusal(issue_link(world.session)) == NOT_READY
    await assert_nothing_issued()


async def test_a_person_without_any_access_gets_no_access(world: World) -> None:
    user = VPNUser(username="lonely", email="lonely@example.org", telegram_id=TELEGRAM_ID)
    world.session.add(user)
    await world.session.commit()

    assert await refusal(issue_link(world.session)) == NO_ACCESS
    await assert_nothing_issued()


async def test_a_revoked_grant_whose_client_is_still_on_the_server_gets_no_access(world: World) -> None:
    _, _, grant, credential = await issued_user(world)
    await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(grant.id)  # задача REVOKE_ACCESS ещё не выполнена
    assert credential.revoked_at is None

    assert await refusal(issue_link(world.session)) == NO_ACCESS
    await assert_nothing_issued()


async def test_an_overdue_subscription_gets_no_access_without_the_scheduler(world: World) -> None:
    _, subscription, _, _ = await issued_user(world)
    subscription.valid_from = datetime.now(timezone.utc) - timedelta(days=3)
    subscription.valid_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await world.session.commit()

    assert await refusal(issue_link(world.session)) == NO_ACCESS
    await assert_nothing_issued()


async def test_an_unknown_telegram_id_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await issue_link(world.session, 5559990001)
    await assert_nothing_issued()


# --- выпуск: аудит -------------------------------------------------------------------------------------------------


async def test_issuing_writes_one_audit_event_with_the_agreed_meta(world: World) -> None:
    user, _, _, credential = await issued_user(world)
    await issue_link(world.session)

    (event,) = await world.audit(AuditAction(NEW_AUDIT))
    assert event.meta == {
        "credential_id": str(credential.id),
        "user_id": str(user.id),
        "source": "bot",
        "replaced": False,
    }


async def test_the_second_issue_says_it_replaced_a_link(world: World) -> None:
    await issued_user(world)
    await issue_link(world.session)
    await issue_link(world.session)

    events = await world.audit(AuditAction(NEW_AUDIT))
    assert sorted(bool(event.meta["replaced"]) for event in events) == [False, True]


async def test_the_audit_has_neither_the_token_nor_its_hash_nor_the_telegram_id(world: World) -> None:
    await issued_user(world)
    issued = await issue_link(world.session)
    await open_link(world.session, issued.token)

    dump = await audit_dump()
    assert NEW_AUDIT in dump
    for secret in (issued.token, hash_token(issued.token), str(TELEGRAM_ID)):
        assert secret not in dump


# --- открытие: что видит человек -----------------------------------------------------------------------------------


async def test_opening_gives_the_config_in_all_forms(world: World) -> None:
    user, subscription, _, credential = await issued_user(world)
    issued = await issue_link(world.session)

    page = await open_link(world.session, issued.token)

    assert page.name == user.username
    assert page.valid_until == subscription.valid_until
    assert page.server_name and page.config == decrypt_secret(credential.encrypted_config or "")
    assert page.config.lstrip().startswith("[Interface]")
    assert page.share_link and page.share_link.startswith("vpn://")
    assert page.share_qr and all(isinstance(code, str) and code for code in page.share_qr)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", page.file_name)


async def test_opening_writes_the_time_of_the_last_open(world: World) -> None:
    await issued_user(world)
    issued = await issue_link(world.session)
    before = datetime.now(timezone.utc)

    await open_link(world.session, issued.token)

    (row,) = await links_in_db()
    assert row.last_opened_at is not None and row.last_opened_at >= before - timedelta(seconds=1)


async def test_opening_writes_no_audit_event(world: World) -> None:
    await issued_user(world)
    issued = await issue_link(world.session)
    before = await audit_dump()

    await open_link(world.session, issued.token)
    await open_link(world.session, issued.token)

    assert await audit_dump() == before  # ни config_viewed, ни чего-либо ещё: открытий много, это не действие админа


async def test_opening_twice_works_the_link_is_not_one_time(world: World) -> None:
    await issued_user(world)
    issued = await issue_link(world.session)

    first = await open_link(world.session, issued.token)
    second = await open_link(world.session, issued.token)

    assert first.config == second.config


async def test_render_config_writes_no_audit_but_reveal_config_still_does(world: World) -> None:
    _, _, grant, credential = await issued_user(world)
    service = world.credentials()

    rendered = await service.render_config(credential, grant)
    assert rendered.config and rendered.share_link and rendered.share_qr
    assert await world.audit(AuditAction.CONFIG_VIEWED) == []

    revealed = await service.reveal_config(credential.id)
    assert revealed.config == rendered.config and revealed.share_link == rendered.share_link
    assert len(await world.audit(AuditAction.CONFIG_VIEWED)) == 1


# --- открытие: всё остальное - одинаковый отказ --------------------------------------------------------------------


@pytest.mark.parametrize("token", ["", "short", "x" * 42, "x" * 44, "!" * 43, "a b" + "x" * 40, "x" * 42 + "é"])
async def test_a_token_of_the_wrong_shape_is_not_found(world: World, token: str) -> None:
    await issued_user(world)
    await issue_link(world.session)
    await assert_not_found(world, token)


async def test_a_well_formed_unknown_token_is_not_found(world: World) -> None:
    await issued_user(world)
    await issue_link(world.session)
    await assert_not_found(world, "Z" * 43)


async def test_a_revoked_link_row_is_not_found(world: World) -> None:
    await issued_user(world)
    issued = await issue_link(world.session)
    (row,) = await links_in_db()
    async with session_factory() as session:
        await session.execute(text("UPDATE config_links SET revoked_at = now() WHERE id = :id"), {"id": row.id})
        await session.commit()
    await assert_not_found(world, issued.token)


async def test_a_revoked_client_closes_the_link(world: World) -> None:
    _, _, _, credential = await issued_user(world)
    issued = await issue_link(world.session)
    credential.revoked_at = datetime.now(timezone.utc)
    await world.session.commit()

    await assert_not_found(world, issued.token)


async def test_a_revoked_grant_closes_the_link_before_the_revoke_job_runs(world: World) -> None:
    _, _, grant, _ = await issued_user(world)
    issued = await issue_link(world.session)

    await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(grant.id)

    assert len(await world.jobs(JobType.REVOKE_ACCESS)) == 1  # задача ещё не выполнялась
    await assert_not_found(world, issued.token)


async def test_a_revoked_subscription_closes_the_link(world: World) -> None:
    _, subscription, _, _ = await issued_user(world)
    issued = await issue_link(world.session)

    await SubscriptionService(world.session, SYSTEM_ACTOR).revoke(subscription.id)

    await assert_not_found(world, issued.token)


async def test_an_expired_period_closes_the_link_without_the_scheduler(world: World) -> None:
    _, subscription, _, _ = await issued_user(world)
    issued = await issue_link(world.session)
    subscription.valid_from = datetime.now(timezone.utc) - timedelta(days=3)
    subscription.valid_until = datetime.now(timezone.utc) - timedelta(milliseconds=1)
    await world.session.commit()

    await assert_not_found(world, issued.token)


async def test_a_link_to_an_imported_client_is_not_found(world: World) -> None:
    from modules.bot.models.config_link import ConfigLink

    _, _, _, credential = await issued_user(world)
    credential.source = CredentialSource.IMPORTED
    credential.encrypted_config = None
    token = "I" * 43
    world.session.add(ConfigLink(token_hash=hash_token(token), credential_id=credential.id))
    await world.session.commit()

    await assert_not_found(world, token)


async def test_deleting_the_client_deletes_its_links(world: World) -> None:
    _, _, _, credential = await issued_user(world)
    issued = await issue_link(world.session)

    async with session_factory() as session:
        await session.execute(text("DELETE FROM vpn_credentials WHERE id = :id"), {"id": credential.id})
        await session.commit()

    assert await links_in_db() == []
    await assert_not_found(world, issued.token)


async def test_the_config_is_not_decrypted_for_a_refused_link(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _, subscription, _, _ = await issued_user(world)
    issued = await issue_link(world.session)
    subscription.valid_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await world.session.commit()
    calls: list[str] = []

    import modules.access.service as access_service

    real = access_service.decrypt_secret
    monkeypatch.setattr(access_service, "decrypt_secret", lambda value: calls.append("decrypt") or real(value))

    await assert_not_found(world, issued.token)
    assert calls == []


async def test_a_refused_open_does_not_touch_last_opened_at(world: World) -> None:
    await issued_user(world)
    first = await issue_link(world.session)
    await issue_link(world.session)  # первая ссылка заменена

    await assert_not_found(world, first.token)

    rows = await links_in_db()
    assert all(row.last_opened_at is None for row in rows)
