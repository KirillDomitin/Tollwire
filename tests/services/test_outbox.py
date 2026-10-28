"""Исходящая очередь бота и рассылки (этап 25.4): постановка, выдача пачки боту (`claim`), подтверждение (`ack`).

Настоящие Postgres и Redis, SSH фейковый, Telegram не участвует: бота здесь играет сам тест. Главное:
- ссылка на конфиг выпускается только при выдаче строки боту, токена нет ни в одной колонке очереди и аудита (Р1-A);
- темп отправки панель не держит (Р2-A): `claim` ограничен лишь размером пачки и временем аренды;
- счётчики рассылки меняются в той же транзакции, что статус строки, и сходятся (Р4-A);
- аренда пачки: `lease_id` на каждый `claim`, чужое подтверждение не принимается.

Допущения об именах - в блоке 25.4 файла `tests/bot_support.py`."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.postgres import session_factory
from core.exceptions import InvalidDataError, NotFoundError, ReasonConflictError
from core.security import hash_token
from modules.access.models import CredentialSource
from modules.access.schemas import AccessGrantCreate
from modules.access.service import AccessGrantService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.bot.models.key import BotKey
from tests.bot_support import (
    BLOCKED,
    BOT_BLOCKED,
    CANCELLED,
    CLAIMED,
    CONFIG,
    CONFIG_UNAVAILABLE,
    FAILED,
    NEWS,
    NEWS_MARKER,
    NEWS_TEXT,
    NO_ACCESS,
    NO_TELEGRAM,
    NOT_CONFIGURED,
    NOT_READY,
    NOT_READY_GIVE_UP_MINUTES,
    PENDING,
    PRIORITY_BROADCAST,
    PRIORITY_INDIVIDUAL,
    PUBLIC_BASE,
    SECOND_TELEGRAM_ID,
    SENT,
    SKIPPED,
    TARGET_ALL,
    TARGET_PROTOCOL,
    TARGET_SELECTED,
    TELEGRAM_ID,
    TOKEN_SHAPE,
    TOO_MANY_ATTEMPTS,
    ack,
    ack_all,
    ack_item,
    add_people,
    add_person,
    assert_counters_consistent,
    audience_of,
    audit_dump,
    broadcast_row,
    cancel_broadcast,
    claim,
    counters,
    create_broadcast,
    create_key,
    expire_lease,
    issue_link,
    issued_person,
    lease_seconds,
    links_in_db,
    live_links_in_db,
    make_ready,
    max_attempts,
    open_link,
    outbox_row,
    outbox_rows,
    queue_link,
    registration_service,
    set_public_address,
    sql,
    sql_rows,
    status_value,
    statuses,
    tables_dump,
    token_of,
    user_row,
)
from tests.fixtures import World

AUDIT_QUEUED = "bot_config_link_queued"
AUDIT_LINK = "config_link_created"
SECONDS = timedelta(seconds=1)


@pytest.fixture
async def key(session: AsyncSession) -> BotKey:
    created, _ = await create_key(session)
    return created


def near(value: datetime, expected: datetime, within: timedelta = timedelta(seconds=10)) -> bool:
    return abs(value - expected) <= within


def in_a_while(**delta: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**delta)


async def events(action: str) -> list[AuditLog]:
    async with session_factory() as session:
        return list((await session.execute(select(AuditLog).where(AuditLog.action == AuditAction(action)))).scalars())


async def with_telegram(world: World, user, telegram_id: int):  # type: ignore[no-untyped-def]
    user.telegram_id = telegram_id
    await world.session.commit()
    return user


async def news_for(world: World, count: int = 3, **fields):  # type: ignore[no-untyped-def]
    """Рассылка новости всем: `count` человек с Telegram."""
    people = await add_people(world.session, count)
    broadcast = await create_broadcast(world.session, **fields)
    return people, broadcast


async def refusal(coro) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(ReasonConflictError) as caught:
        await coro
    return str(caught.value.reason)


async def waited(minutes: int) -> None:
    """Строка стоит в очереди уже `minutes` минут и готова к выдаче."""
    await sql(
        "UPDATE bot_outbox SET created_at = now() - make_interval(mins => :m), "
        "not_before = now() - interval '1 second'",
        m=minutes,
    )


async def age_rows(days: int) -> None:
    """Все строки очереди состарены на `days` дней (по всем временным колонкам)."""
    await sql(
        "UPDATE bot_outbox SET created_at = now() - make_interval(days => :d), "
        "updated_at = now() - make_interval(days => :d), "
        "sent_at = CASE WHEN sent_at IS NULL THEN NULL ELSE now() - make_interval(days => :d) END",
        d=days,
    )


async def schedule_issue_in_progress(world: World):  # type: ignore[no-untyped-def]
    """Человек, у которого клиента ещё выдают (CREATE_USER в очереди): ссылку выпустить нельзя, но скоро можно."""
    _, protocol, _ = await world.server()
    assert protocol is not None
    user, _, grant = await world.access(protocol)
    await with_telegram(world, user, TELEGRAM_ID)
    await world.credentials().schedule_issue(grant.id)
    return user


# --- постановка: кому ----------------------------------------------------------------------------------------------


async def test_news_for_everyone_queues_a_row_for_each_person_with_telegram(world: World) -> None:
    a, b = await add_person(world.session, 5550000001, "A"), await add_person(world.session, 5550000002, "B")
    await add_person(world.session, None, "No telegram")
    await add_person(world.session, 5550000003, "Blocked", blocked=True)

    broadcast = await create_broadcast(world.session)

    rows = await outbox_rows(broadcast_id=broadcast.id)
    assert {row.user_id for row in rows} == {a.id, b.id}
    assert statuses(rows) == [PENDING, PENDING] and counters(await broadcast_row(broadcast.id))["total"] == 2


async def test_a_queued_row_is_pending_with_the_broadcast_priority_and_no_lease(world: World) -> None:
    await add_people(world.session, 1)
    broadcast = await create_broadcast(world.session)

    (row,) = await outbox_rows(broadcast_id=broadcast.id)

    assert (status_value(row.kind), row.priority, row.attempts) == (NEWS, PRIORITY_BROADCAST, 0)
    assert row.lease_id is None and row.claimed_until is None and row.claimed_by_key_id is None
    assert row.config_link_id is None and row.error is None and row.sent_at is None


async def test_the_news_text_is_kept_once_in_the_broadcast_and_not_in_the_rows(world: World) -> None:
    await add_people(world.session, 3)

    broadcast = await create_broadcast(world.session)

    assert (await broadcast_row(broadcast.id)).text == NEWS_TEXT
    assert NEWS_MARKER not in await tables_dump("bot_outbox")


async def test_news_for_selected_people_skips_the_blocked_and_those_without_telegram(world: World) -> None:
    a, b = await add_person(world.session, 5550000001), await add_person(world.session, 5550000002)
    blocked = await add_person(world.session, 5550000003, blocked=True)
    lonely = await add_person(world.session, None)
    outsider = await add_person(world.session, 5550000004)

    broadcast = await create_broadcast(
        world.session, target=TARGET_SELECTED, user_ids=[a.id, b.id, blocked.id, lonely.id, uuid4()]
    )

    assert {row.user_id for row in await outbox_rows(broadcast_id=broadcast.id)} == {a.id, b.id}
    assert outsider.id not in {row.user_id for row in await outbox_rows()}
    assert counters(await broadcast_row(broadcast.id))["total"] == 2


async def test_a_person_listed_twice_gets_one_row(world: World) -> None:
    person = await add_person(world.session)

    broadcast = await create_broadcast(world.session, target=TARGET_SELECTED, user_ids=[person.id, person.id])

    assert len(await outbox_rows(broadcast_id=broadcast.id)) == 1
    assert counters(await broadcast_row(broadcast.id))["total"] == 1


async def test_the_same_person_may_be_in_two_different_broadcasts(world: World) -> None:
    person = await add_person(world.session)

    first = await create_broadcast(world.session, target=TARGET_SELECTED, user_ids=[person.id])
    second = await create_broadcast(world.session, target=TARGET_SELECTED, user_ids=[person.id])

    assert {row.broadcast_id for row in await outbox_rows(user_id=person.id)} == {first.id, second.id}


async def test_news_for_a_protocol_goes_to_people_with_a_live_grant_on_it_and_remembers_the_grant(
    world: World,
) -> None:
    _, mine, _ = await world.server()
    _, other, _ = await world.server()
    assert mine is not None and other is not None
    user_a, _, grant_a = await world.access(mine)
    user_b, _, grant_b = await world.access(mine)
    user_elsewhere, _, _ = await world.access(other)
    user_revoked, _, grant_revoked = await world.access(mine)
    for number, user in enumerate((user_a, user_b, user_elsewhere, user_revoked)):
        await with_telegram(world, user, 5550000100 + number)
    await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(grant_revoked.id)

    broadcast = await create_broadcast(world.session, target=TARGET_PROTOCOL, protocol_id=mine.id)

    rows = await outbox_rows(broadcast_id=broadcast.id)
    assert {row.user_id: row.access_grant_id for row in rows} == {user_a.id: grant_a.id, user_b.id: grant_b.id}


async def test_a_person_with_grants_on_two_protocols_is_in_the_protocol_broadcast_once(world: World) -> None:
    _, first, _ = await world.server()
    _, second, _ = await world.server()
    assert first is not None and second is not None
    user, subscription, grant = await world.access(first)
    await with_telegram(world, user, TELEGRAM_ID)
    await AccessGrantService(world.session, SYSTEM_ACTOR).create(
        subscription.id, AccessGrantCreate(protocol_id=second.id)
    )

    broadcast = await create_broadcast(world.session, target=TARGET_PROTOCOL, protocol_id=first.id)

    (row,) = await outbox_rows(broadcast_id=broadcast.id)
    assert row.user_id == user.id and row.access_grant_id == grant.id


async def test_the_same_request_id_returns_the_same_broadcast_and_queues_nothing_twice(world: World) -> None:
    from tests.bot_support import broadcast_data, outbox_service

    await add_people(world.session, 3)
    data = broadcast_data()

    first = await outbox_service(world.session).create_broadcast(data)
    second = await outbox_service(world.session).create_broadcast(data)

    assert first.id == second.id
    assert len(await outbox_rows()) == 3 and counters(await broadcast_row(first.id))["total"] == 3
    assert len(await events("bot_broadcast_created")) == 1


async def test_a_second_open_config_message_for_the_same_person_is_not_queued(world: World) -> None:
    person = await add_person(world.session)
    first = await create_broadcast(world.session, kind=CONFIG, target=TARGET_SELECTED, user_ids=[person.id])

    second = await create_broadcast(world.session, kind=CONFIG, target=TARGET_SELECTED, user_ids=[person.id])

    assert counters(await broadcast_row(first.id))["total"] == 1
    assert counters(await broadcast_row(second.id))["total"] == 0  # двух живых отправок ссылки одному человеку нет
    assert len(await outbox_rows(kind=CONFIG)) == 1


async def test_news_may_be_queued_next_to_an_open_config_message(world: World) -> None:
    person = await add_person(world.session)
    await create_broadcast(world.session, kind=CONFIG, target=TARGET_SELECTED, user_ids=[person.id])

    news = await create_broadcast(world.session, target=TARGET_SELECTED, user_ids=[person.id])

    assert counters(await broadcast_row(news.id))["total"] == 1


async def test_config_links_cannot_be_queued_while_the_panel_address_is_not_set(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    person = await add_person(world.session)
    set_public_address(monkeypatch, "", domain="")

    assert (
        await refusal(create_broadcast(world.session, kind=CONFIG, target=TARGET_SELECTED, user_ids=[person.id]))
        == NOT_CONFIGURED
    )

    assert await outbox_rows() == [] and await sql_rows("SELECT id FROM bot_broadcasts") == []


async def test_news_does_not_need_the_panel_address(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_people(world.session, 2)
    set_public_address(monkeypatch, "", domain="")

    broadcast = await create_broadcast(world.session)

    assert counters(await broadcast_row(broadcast.id))["total"] == 2


async def test_more_recipients_than_the_limit_are_refused_and_nothing_is_queued(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_people(world.session, 4)
    monkeypatch.setattr(settings, "bot_broadcast_max_recipients", 3)

    with pytest.raises(InvalidDataError):
        await create_broadcast(world.session)

    assert await outbox_rows() == [] and await sql_rows("SELECT id FROM bot_broadcasts") == []


async def test_exactly_the_limit_is_accepted(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_people(world.session, 3)
    monkeypatch.setattr(settings, "bot_broadcast_max_recipients", 3)

    broadcast = await create_broadcast(world.session)

    assert counters(await broadcast_row(broadcast.id))["total"] == 3


async def test_the_audience_counts_recipients_people_without_telegram_and_the_blocked(world: World) -> None:
    await add_people(world.session, 2)
    await add_person(world.session, 5550000009, blocked=True)
    await add_person(world.session, None)

    audience = await audience_of(world.session)

    assert (audience.recipients, audience.without_telegram, audience.blocked) == (2, 1, 1)


async def test_asking_for_the_audience_writes_nothing(world: World) -> None:
    await add_people(world.session, 2)

    await audience_of(world.session)

    assert await outbox_rows() == [] and await sql_rows("SELECT id FROM bot_broadcasts") == []
    assert "bot_broadcast" not in await audit_dump()


async def test_the_audience_of_selected_people_counts_only_them(world: World) -> None:
    chosen = await add_person(world.session, 5550000001)
    blocked = await add_person(world.session, 5550000002, blocked=True)
    lonely = await add_person(world.session, None)
    await add_person(world.session, 5550000003)

    audience = await audience_of(world.session, target=TARGET_SELECTED, user_ids=[chosen.id, blocked.id, lonely.id])

    assert (audience.recipients, audience.without_telegram, audience.blocked) == (1, 1, 1)


async def test_the_audience_matches_what_a_broadcast_then_queues(world: World) -> None:
    await add_people(world.session, 4)
    await add_person(world.session, 5550000009, blocked=True)

    audience = await audience_of(world.session)
    broadcast = await create_broadcast(world.session)

    assert audience.recipients == counters(await broadcast_row(broadcast.id))["total"] == 4


# --- постановка: аудит ---------------------------------------------------------------------------------------------


async def test_creating_a_broadcast_writes_one_audit_event_without_text_and_people(world: World) -> None:
    people = await add_people(world.session, 2)

    broadcast = await create_broadcast(world.session)

    (event,) = await events("bot_broadcast_created")
    assert event.entity_type == "bot_broadcast" and event.entity_id == broadcast.id
    assert event.meta["kind"] == NEWS and event.meta["target"] == TARGET_ALL and event.meta["recipients"] == 2
    assert isinstance(event.meta["text_length"], int) and event.meta["text_length"] > 0
    assert set(event.meta) <= {"kind", "target", "protocol_id", "recipients", "text_length"}
    dump = await audit_dump()
    assert NEWS_MARKER not in dump
    assert all(str(person.telegram_id) not in dump and str(person.id) not in dump for person in people)


async def test_the_audit_of_a_protocol_broadcast_names_the_protocol(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    user, _, _ = await world.access(protocol)
    await with_telegram(world, user, TELEGRAM_ID)

    await create_broadcast(world.session, target=TARGET_PROTOCOL, protocol_id=protocol.id)

    (event,) = await events("bot_broadcast_created")
    assert event.meta["protocol_id"] == str(protocol.id) and event.meta["target"] == TARGET_PROTOCOL


# --- выдача пачки: claim -------------------------------------------------------------------------------------------


async def test_claiming_an_empty_queue_gives_nothing_and_still_marks_that_the_bot_asked(
    world: World, key: BotKey
) -> None:
    before = datetime.now(timezone.utc)

    claimed = await claim(world.session, key)

    assert list(claimed.items) == [] and dict(claimed.texts) == {}
    (stored,) = await sql_rows("SELECT last_claim_at FROM bot_keys WHERE id = :id", id=key.id)
    assert stored[0] is not None and stored[0] >= before - SECONDS


async def test_a_claim_hands_out_news_rows_with_the_address_of_each_person(world: World, key: BotKey) -> None:
    people, broadcast = await news_for(world, 3)

    claimed = await claim(world.session, key)

    assert {item.telegram_id for item in claimed.items} == {person.telegram_id for person in people}
    assert {status_value(item.kind) for item in claimed.items} == {NEWS}
    assert {item.broadcast_id for item in claimed.items} == {broadcast.id}


async def test_the_texts_come_once_per_broadcast_keyed_by_its_id(world: World, key: BotKey) -> None:
    await add_people(world.session, 3)
    first = await create_broadcast(world.session)
    second = await create_broadcast(world.session, text="<i>Вторая новость</i>")

    claimed = await claim(world.session, key)

    assert {str(broadcast_id): value for broadcast_id, value in claimed.texts.items()} == {
        str(first.id): NEWS_TEXT,
        str(second.id): "<i>Вторая новость</i>",
    }


async def test_a_claim_leases_the_rows_to_the_asking_key_for_the_lease_time(world: World, key: BotKey) -> None:
    await news_for(world, 2)

    claimed = await claim(world.session, key)

    rows = await outbox_rows()
    assert statuses(rows) == [CLAIMED, CLAIMED]
    for row in rows:
        assert row.lease_id == claimed.lease_id and row.claimed_by_key_id == key.id and row.attempts == 0
        assert near(row.claimed_until, in_a_while(seconds=lease_seconds()))
    assert near(claimed.lease_until, in_a_while(seconds=lease_seconds()))


async def test_every_claim_gets_its_own_lease(world: World, key: BotKey) -> None:
    await news_for(world, 4)

    first, second = await claim(world.session, key, 2), await claim(world.session, key, 2)

    assert first.lease_id != second.lease_id
    assert {item.id for item in first.items}.isdisjoint({item.id for item in second.items})
    assert len(first.items) == len(second.items) == 2


async def test_the_limit_of_a_claim_is_respected_and_the_rest_waits(world: World, key: BotKey) -> None:
    await news_for(world, 5)

    claimed = await claim(world.session, key, 2)

    assert len(claimed.items) == 2
    assert sorted(statuses(await outbox_rows())) == [CLAIMED, CLAIMED, PENDING, PENDING, PENDING]


async def test_rows_that_are_already_leased_are_not_handed_out_again(world: World, key: BotKey) -> None:
    await news_for(world, 3)
    await claim(world.session, key)

    assert list((await claim(world.session, key)).items) == []


async def test_an_individual_message_goes_before_a_big_broadcast_even_if_queued_later(
    world: World, key: BotKey
) -> None:
    await add_people(world.session, 3, first_telegram_id=5560000000)
    await create_broadcast(world.session)
    user, _, _, _ = await issued_person(world)
    queued = await queue_link(world.session, user.id)

    claimed = await claim(world.session, key, 1)

    assert [item.id for item in claimed.items] == [queued.id]
    assert (await outbox_row(queued.id)).priority == PRIORITY_INDIVIDUAL


async def test_earlier_broadcasts_go_first_within_the_same_priority(world: World, key: BotKey) -> None:
    await add_people(world.session, 2)
    first = await create_broadcast(world.session)
    await create_broadcast(world.session)  # те же двое, поставлено позже

    claimed = await claim(world.session, key, 2)

    assert {item.broadcast_id for item in claimed.items} == {first.id}


async def test_a_row_postponed_by_not_before_is_not_handed_out_until_its_time(world: World, key: BotKey) -> None:
    await news_for(world, 1)
    await sql("UPDATE bot_outbox SET not_before = now() + interval '1 hour'")

    assert list((await claim(world.session, key)).items) == []

    await make_ready()
    assert len((await claim(world.session, key)).items) == 1


async def test_a_claim_skips_rows_locked_by_another_claim_that_is_still_running(world: World, key: BotKey) -> None:
    await news_for(world, 5)
    async with session_factory() as rival:
        locked = (
            (
                await rival.execute(
                    text(
                        "SELECT id FROM bot_outbox WHERE status = 'pending' ORDER BY priority, created_at, id "
                        "LIMIT 3 FOR UPDATE"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(locked) == 3

        claimed = await asyncio.wait_for(claim(world.session, key, 10), timeout=10)  # не ждёт чужую блокировку

        got = {item.id for item in claimed.items}
        assert len(got) == 2 and got.isdisjoint(locked)
        await rival.rollback()

    assert {item.id for item in (await claim(world.session, key, 10)).items} == set(locked)


async def test_a_row_whose_lease_ran_out_is_handed_out_again_with_one_more_attempt(world: World, key: BotKey) -> None:
    await news_for(world, 2)
    first = await claim(world.session, key)
    await expire_lease()

    second = await claim(world.session, key)

    assert {item.id for item in second.items} == {item.id for item in first.items}
    assert second.lease_id != first.lease_id
    for row in await outbox_rows():
        assert row.lease_id == second.lease_id and row.attempts == 1 and status_value(row.status) == CLAIMED


async def test_a_lease_that_has_not_run_out_is_not_taken_away(world: World, key: BotKey) -> None:
    await news_for(world, 2)
    first = await claim(world.session, key)
    await sql("UPDATE bot_outbox SET claimed_until = now() + interval '5 seconds'")

    assert list((await claim(world.session, key)).items) == []
    assert {row.lease_id for row in await outbox_rows()} == {first.lease_id}


async def test_a_person_who_lost_the_telegram_link_before_the_claim_is_skipped(world: World, key: BotKey) -> None:
    people, broadcast = await news_for(world, 2)
    await sql("UPDATE vpn_users SET telegram_id = NULL WHERE id = :id", id=people[0].id)

    claimed = await claim(world.session, key)

    assert [item.telegram_id for item in claimed.items] == [people[1].telegram_id]
    skipped = await outbox_rows(user_id=people[0].id)
    assert statuses(skipped) == [SKIPPED] and skipped[0].error == NO_TELEGRAM
    assert counters(await broadcast_row(broadcast.id))["skipped"] == 1
    await assert_counters_consistent(broadcast.id)


async def test_a_person_who_blocked_the_bot_before_the_claim_is_closed_without_sending(
    world: World, key: BotKey
) -> None:
    people, broadcast = await news_for(world, 2)
    await sql("UPDATE vpn_users SET bot_blocked_at = now() WHERE id = :id", id=people[0].id)

    claimed = await claim(world.session, key)

    assert [item.telegram_id for item in claimed.items] == [people[1].telegram_id]
    assert statuses(await outbox_rows(user_id=people[0].id)) == [BLOCKED]
    assert counters(await broadcast_row(broadcast.id))["blocked"] == 1
    await assert_counters_consistent(broadcast.id)


# --- предел попыток ------------------------------------------------------------------------------------------------


async def test_a_pending_row_that_used_up_its_attempts_fails_and_is_not_handed_out(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 1)
    await sql("UPDATE bot_outbox SET attempts = :n", n=max_attempts())

    claimed = await claim(world.session, key)

    assert list(claimed.items) == []
    (row,) = await outbox_rows()
    assert status_value(row.status) == FAILED and row.error == TOO_MANY_ATTEMPTS
    assert counters(await broadcast_row(broadcast.id))["failed"] == 1


async def test_a_row_with_one_attempt_left_is_still_handed_out(world: World, key: BotKey) -> None:
    await news_for(world, 1)
    await sql("UPDATE bot_outbox SET attempts = :n", n=max_attempts() - 1)

    assert len((await claim(world.session, key)).items) == 1


async def test_a_lease_that_keeps_running_out_ends_in_too_many_attempts(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 1)
    await claim(world.session, key)
    await sql("UPDATE bot_outbox SET attempts = :n", n=max_attempts() - 1)
    await expire_lease()

    assert list((await claim(world.session, key)).items) == []  # эта выдача была бы последней, лишней

    (row,) = await outbox_rows()
    assert status_value(row.status) == FAILED and row.error == TOO_MANY_ATTEMPTS
    await assert_counters_consistent(broadcast.id)


async def test_a_lease_that_ran_out_with_attempts_to_spare_is_handed_out_again(world: World, key: BotKey) -> None:
    await news_for(world, 1)
    await claim(world.session, key)
    await sql("UPDATE bot_outbox SET attempts = :n", n=max_attempts() - 2)
    await expire_lease()

    assert len((await claim(world.session, key)).items) == 1
    assert (await outbox_rows())[0].attempts == max_attempts() - 1


# --- подтверждение: ack --------------------------------------------------------------------------------------------


async def test_sent_closes_the_row_with_the_time_and_counts_in_the_broadcast(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 2)
    claimed = await claim(world.session, key)

    result = await ack_all(world.session, claimed)

    assert set(result.applied) == {item.id for item in claimed.items} and list(result.ignored) == []
    for row in await outbox_rows():
        assert status_value(row.status) == SENT and row.sent_at is not None
    assert counters(await broadcast_row(broadcast.id))["sent"] == 2
    await assert_counters_consistent(broadcast.id)


async def test_an_ack_with_a_foreign_lease_changes_nothing_and_names_the_rows_as_ignored(
    world: World, key: BotKey
) -> None:
    _, broadcast = await news_for(world, 2)
    claimed = await claim(world.session, key)

    result = await ack(world.session, uuid4(), *[ack_item(item.id, SENT) for item in claimed.items])

    assert list(result.applied) == [] and set(result.ignored) == {item.id for item in claimed.items}
    assert statuses(await outbox_rows()) == [CLAIMED, CLAIMED]
    assert counters(await broadcast_row(broadcast.id))["sent"] == 0


async def test_an_ack_for_unknown_rows_is_ignored(world: World, key: BotKey) -> None:
    await news_for(world, 1)
    claimed = await claim(world.session, key)
    unknown = uuid4()

    result = await ack(world.session, claimed.lease_id, ack_item(unknown, SENT))

    assert list(result.applied) == [] and list(result.ignored) == [unknown]


async def test_a_mixed_ack_applies_its_own_rows_and_ignores_the_rest(world: World, key: BotKey) -> None:
    await news_for(world, 2)
    first = await claim(world.session, key, 1)
    second = await claim(world.session, key, 1)
    mine, theirs = first.items[0], second.items[0]

    result = await ack(world.session, first.lease_id, ack_item(mine.id, SENT), ack_item(theirs.id, SENT))

    assert list(result.applied) == [mine.id] and list(result.ignored) == [theirs.id]
    assert status_value((await outbox_row(theirs.id)).status) == CLAIMED


async def test_repeating_an_ack_changes_nothing(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 2)
    claimed = await claim(world.session, key)
    await ack_all(world.session, claimed)
    before = (await broadcast_row(broadcast.id), [row.sent_at for row in await outbox_rows()])

    await ack_all(world.session, claimed)

    after = (await broadcast_row(broadcast.id), [row.sent_at for row in await outbox_rows()])
    assert counters(after[0]) == counters(before[0]) and after[1] == before[1]
    assert counters(after[0])["sent"] == 2


async def test_a_repeated_ack_cannot_turn_a_sent_row_into_a_failed_one(world: World, key: BotKey) -> None:
    await news_for(world, 1)
    claimed = await claim(world.session, key)
    await ack_all(world.session, claimed)

    await ack(world.session, claimed.lease_id, ack_item(claimed.items[0].id, FAILED, error="bad_request"))

    assert statuses(await outbox_rows()) == [SENT]


async def test_an_ack_after_the_lease_ran_out_is_accepted_while_nobody_else_got_the_rows(
    world: World, key: BotKey
) -> None:
    await news_for(world, 2)
    claimed = await claim(world.session, key)
    await expire_lease()

    result = await ack_all(world.session, claimed)

    assert len(result.applied) == 2 and statuses(await outbox_rows()) == [SENT, SENT]


async def test_an_ack_after_the_rows_went_to_another_lease_is_ignored(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 2)
    old = await claim(world.session, key)
    await expire_lease()
    new = await claim(world.session, key)

    result = await ack_all(world.session, old)

    assert list(result.applied) == [] and len(result.ignored) == 2
    assert {row.lease_id for row in await outbox_rows()} == {new.lease_id}
    assert counters(await broadcast_row(broadcast.id))["sent"] == 0
    assert len((await ack_all(world.session, new)).applied) == 2  # новая аренда подтверждается как обычно


async def test_blocked_marks_the_person_and_counts_in_the_broadcast(world: World, key: BotKey) -> None:
    people, broadcast = await news_for(world, 2)
    claimed = await claim(world.session, key)
    blocker = next(item for item in claimed.items if item.telegram_id == people[0].telegram_id)

    await ack(
        world.session,
        claimed.lease_id,
        ack_item(blocker.id, BLOCKED, error="blocked"),
        *[ack_item(item.id, SENT) for item in claimed.items if item.id != blocker.id],
    )

    assert (await user_row(people[0].id)).bot_blocked_at is not None
    assert (await user_row(people[1].id)).bot_blocked_at is None
    assert counters(await broadcast_row(broadcast.id))["blocked"] == 1
    await assert_counters_consistent(broadcast.id)


async def test_after_a_block_the_other_messages_of_that_person_are_closed_without_sending(
    world: World, key: BotKey
) -> None:
    person = await add_person(world.session)
    first = await create_broadcast(world.session)
    claimed = await claim(world.session, key)
    second = await create_broadcast(world.session)  # поставлена до того, как пришло подтверждение

    await ack(world.session, claimed.lease_id, ack_item(claimed.items[0].id, BLOCKED, error="blocked"))
    follow_up = await claim(world.session, key)

    assert list(follow_up.items) == []
    assert statuses(await outbox_rows(broadcast_id=second.id)) == [BLOCKED]
    assert counters(await broadcast_row(second.id))["blocked"] == 1
    assert (await user_row(person.id)).bot_blocked_at is not None
    await assert_counters_consistent(first.id)
    await assert_counters_consistent(second.id)


async def test_failed_keeps_the_error_code_and_counts_in_the_broadcast(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 1)
    claimed = await claim(world.session, key)

    await ack(world.session, claimed.lease_id, ack_item(claimed.items[0].id, FAILED, error="chat_not_found"))

    (row,) = await outbox_rows()
    assert status_value(row.status) == FAILED and row.error == "chat_not_found"
    assert counters(await broadcast_row(broadcast.id))["failed"] == 1


@pytest.mark.parametrize(("retry_after", "at_least"), [(None, 5), (2, 5), (5, 5), (30, 30)])
async def test_retry_puts_the_row_back_not_earlier_than_five_seconds_or_retry_after(
    world: World, key: BotKey, retry_after: int | None, at_least: int
) -> None:
    await news_for(world, 1)
    claimed = await claim(world.session, key)

    await ack(
        world.session,
        claimed.lease_id,
        ack_item(claimed.items[0].id, "retry", error="network", retry_after=retry_after),
    )

    (row,) = await outbox_rows()
    assert status_value(row.status) == PENDING and row.lease_id is None and row.attempts == 1
    assert in_a_while(seconds=at_least) - timedelta(seconds=2) <= row.not_before <= in_a_while(seconds=at_least + 5)


async def test_a_retried_row_waits_for_its_time_and_comes_back_with_the_attempt_counted(
    world: World, key: BotKey
) -> None:
    await news_for(world, 1)
    first = await claim(world.session, key)
    await ack(world.session, first.lease_id, ack_item(first.items[0].id, "retry", error="telegram_5xx"))

    assert list((await claim(world.session, key)).items) == []
    await make_ready()
    again = await claim(world.session, key)

    assert [item.id for item in again.items] == [first.items[0].id]
    assert (await outbox_rows())[0].attempts == 1 and again.lease_id != first.lease_id


async def test_a_flood_retry_does_not_use_up_an_attempt(world: World, key: BotKey) -> None:
    await news_for(world, 1)
    claimed = await claim(world.session, key)

    await ack(world.session, claimed.lease_id, ack_item(claimed.items[0].id, "retry", error="flood", retry_after=20))

    (row,) = await outbox_rows()
    assert status_value(row.status) == PENDING and row.attempts == 0


async def test_a_retry_is_not_counted_in_any_total_and_keeps_the_broadcast_open(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 1)
    claimed = await claim(world.session, key)

    await ack(world.session, claimed.lease_id, ack_item(claimed.items[0].id, "retry", error="network"))

    shown = counters(await broadcast_row(broadcast.id))
    assert shown == {"total": 1, "sent": 0, "blocked": 0, "failed": 0, "skipped": 0, "cancelled": 0}
    assert (await broadcast_row(broadcast.id)).finished_at is None


async def test_one_ack_for_rows_of_two_broadcasts_counts_each_in_its_own(world: World, key: BotKey) -> None:
    await add_people(world.session, 2)
    first = await create_broadcast(world.session)
    second = await create_broadcast(world.session)
    claimed = await claim(world.session, key)

    await ack_all(world.session, claimed)

    assert counters(await broadcast_row(first.id))["sent"] == 2
    assert counters(await broadcast_row(second.id))["sent"] == 2


async def test_a_message_without_a_broadcast_is_acked_like_any_other(world: World, key: BotKey) -> None:
    user, _, _, _ = await issued_person(world)
    queued = await queue_link(world.session, user.id)
    claimed = await claim(world.session, key)

    await ack_all(world.session, claimed)

    assert status_value((await outbox_row(queued.id)).status) == SENT


# --- итог рассылки -------------------------------------------------------------------------------------------------


async def test_the_broadcast_is_finished_when_its_last_open_row_is_resolved(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 3)
    claimed = await claim(world.session, key)
    first, rest = claimed.items[0], claimed.items[1:]

    await ack(world.session, claimed.lease_id, ack_item(first.id, SENT))
    assert (await broadcast_row(broadcast.id)).finished_at is None

    await ack(world.session, claimed.lease_id, *[ack_item(item.id, SENT) for item in rest])
    assert near((await broadcast_row(broadcast.id)).finished_at, datetime.now(timezone.utc))


async def test_a_broadcast_with_rows_still_waiting_is_not_finished(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 3)
    claimed = await claim(world.session, key, 2)

    await ack_all(world.session, claimed)

    assert (await broadcast_row(broadcast.id)).finished_at is None


async def test_the_counters_add_up_through_a_whole_mixed_run(world: World, key: BotKey) -> None:
    people, broadcast = await news_for(world, 6)
    await sql("UPDATE vpn_users SET telegram_id = NULL WHERE id = :id", id=people[0].id)
    await assert_counters_consistent(broadcast.id)

    claimed = await claim(world.session, key)  # 5 выданы, 1 пропущена
    await assert_counters_consistent(broadcast.id)
    ids = [item.id for item in claimed.items]
    await ack(
        world.session,
        claimed.lease_id,
        ack_item(ids[0], SENT),
        ack_item(ids[1], BLOCKED, error="blocked"),
        ack_item(ids[2], FAILED, error="bad_request"),
        ack_item(ids[3], "retry", error="network"),
        ack_item(ids[4], SENT),
    )

    shown = counters(await broadcast_row(broadcast.id))
    assert shown == {"total": 6, "sent": 2, "blocked": 1, "failed": 1, "skipped": 1, "cancelled": 0}
    await assert_counters_consistent(broadcast.id)


async def test_a_deleted_person_drops_out_of_the_broadcast_without_breaking_the_totals(
    world: World, key: BotKey
) -> None:
    people, broadcast = await news_for(world, 3)
    await sql("DELETE FROM vpn_users WHERE id = :id", id=people[0].id)

    claimed = await claim(world.session, key)
    await ack_all(world.session, claimed)

    shown = counters(await broadcast_row(broadcast.id))
    assert shown["total"] == 3 and shown["sent"] == 2
    await assert_counters_consistent(broadcast.id, dropped=1)
    assert (await broadcast_row(broadcast.id)).finished_at is not None


# --- отмена --------------------------------------------------------------------------------------------------------


async def test_cancelling_closes_the_waiting_rows_and_leaves_the_leased_ones(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 5)
    await claim(world.session, key, 2)

    cancelled = await cancel_broadcast(world.session, broadcast.id)

    assert sorted(statuses(await outbox_rows())) == [CANCELLED, CANCELLED, CANCELLED, CLAIMED, CLAIMED]
    stored = await broadcast_row(broadcast.id)
    assert counters(stored)["cancelled"] == 3 and stored.cancelled_at is not None
    assert cancelled.id == broadcast.id
    await assert_counters_consistent(broadcast.id)


async def test_rows_in_the_lease_still_arrive_after_a_cancel_and_the_broadcast_then_finishes(
    world: World, key: BotKey
) -> None:
    _, broadcast = await news_for(world, 4)
    claimed = await claim(world.session, key, 2)
    await cancel_broadcast(world.session, broadcast.id)
    assert (await broadcast_row(broadcast.id)).finished_at is None  # две строки ещё в аренде

    await ack_all(world.session, claimed)

    stored = await broadcast_row(broadcast.id)
    assert counters(stored) == {"total": 4, "sent": 2, "blocked": 0, "failed": 0, "skipped": 0, "cancelled": 2}
    assert stored.finished_at is not None
    await assert_counters_consistent(broadcast.id)


async def test_cancelled_rows_are_never_handed_out(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 3)
    await cancel_broadcast(world.session, broadcast.id)

    assert list((await claim(world.session, key)).items) == []


async def test_cancelling_twice_changes_nothing_and_writes_the_audit_once(world: World, key: BotKey) -> None:
    _, broadcast = await news_for(world, 3)
    await claim(world.session, key, 1)
    await cancel_broadcast(world.session, broadcast.id)
    before = counters(await broadcast_row(broadcast.id))

    await cancel_broadcast(world.session, broadcast.id)

    assert counters(await broadcast_row(broadcast.id)) == before
    assert len(await events("bot_broadcast_cancelled")) == 1


async def test_the_audit_of_a_cancel_counts_the_cancelled_rows_only(world: World) -> None:
    _, broadcast = await news_for(world, 3)

    await cancel_broadcast(world.session, broadcast.id)

    (event,) = await events("bot_broadcast_cancelled")
    assert event.entity_type == "bot_broadcast" and event.entity_id == broadcast.id
    assert event.meta == {"cancelled": 3}


async def test_cancelling_an_unknown_broadcast_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await cancel_broadcast(world.session, uuid4())


async def test_a_cancel_does_not_touch_another_broadcast(world: World) -> None:
    await add_people(world.session, 2)
    first, second = await create_broadcast(world.session), await create_broadcast(world.session)

    await cancel_broadcast(world.session, first.id)

    assert statuses(await outbox_rows(broadcast_id=second.id)) == [PENDING, PENDING]
    assert counters(await broadcast_row(second.id))["cancelled"] == 0


# --- ссылка на конфиг: выпуск только при выдаче --------------------------------------------------------------------


async def config_broadcast(world: World, *people, **fields):  # type: ignore[no-untyped-def]
    return await create_broadcast(
        world.session, kind=CONFIG, target=TARGET_SELECTED, user_ids=[person.id for person in people], **fields
    )


async def test_queueing_config_links_issues_no_link(world: World) -> None:
    user, _, _, _ = await issued_person(world)

    await config_broadcast(world, user)

    assert await links_in_db() == [] and (await outbox_rows())[0].config_link_id is None


async def test_until_the_bot_asks_the_previous_link_keeps_working(world: World) -> None:
    user, _, _, _ = await issued_person(world)
    old = await issue_link(world.session)

    await config_broadcast(world, user)

    assert (await open_link(world.session, old.token)).config


async def test_a_claim_issues_a_fresh_link_for_a_config_row_and_remembers_which(world: World, key: BotKey) -> None:
    user, subscription, _, credential = await issued_person(world)
    await config_broadcast(world, user)

    claimed = await claim(world.session, key)

    (item,) = claimed.items
    assert status_value(item.kind) == CONFIG and item.telegram_id == user.telegram_id
    assert item.config_url.startswith(f"{PUBLIC_BASE}/c#") and TOKEN_SHAPE.fullmatch(token_of(item.config_url))
    assert abs(item.valid_until - subscription.valid_until) <= SECONDS
    (link,) = await live_links_in_db()
    assert link.token_hash == hash_token(token_of(item.config_url)) and link.credential_id == credential.id
    (row,) = await outbox_rows()
    assert row.config_link_id == link.id and status_value(row.status) == CLAIMED


async def test_the_issued_link_opens_the_page(world: World, key: BotKey) -> None:
    user, _, _, _ = await issued_person(world)
    await config_broadcast(world, user)
    (item,) = (await claim(world.session, key)).items

    assert (await open_link(world.session, token_of(item.config_url))).config


async def test_the_token_and_the_address_are_in_no_column_of_the_queue_the_broadcasts_or_the_audit(
    world: World, key: BotKey
) -> None:
    user, _, _, _ = await issued_person(world)
    await config_broadcast(world, user)
    claimed = await claim(world.session, key)
    (item,) = claimed.items
    token = token_of(item.config_url)
    await ack_all(world.session, claimed)

    dump = await tables_dump("bot_outbox", "bot_broadcasts", "audit_logs")

    assert token not in dump and item.config_url not in dump and "/c#" not in dump


async def test_the_audit_of_an_issued_link_says_it_came_from_the_queue(world: World, key: BotKey) -> None:
    user, _, _, credential = await issued_person(world)
    broadcast = await config_broadcast(world, user)

    await claim(world.session, key)

    (event,) = await events(AUDIT_LINK)
    assert event.meta == {
        "credential_id": str(credential.id),
        "user_id": str(user.id),
        "source": "outbox",
        "broadcast_id": str(broadcast.id),
        "replaced": False,
    }


async def test_the_audit_of_an_individual_link_has_no_broadcast(world: World, key: BotKey) -> None:
    user, _, _, _ = await issued_person(world)
    await queue_link(world.session, user.id)

    await claim(world.session, key)

    (event,) = await events(AUDIT_LINK)
    assert event.meta["source"] == "outbox" and event.meta.get("broadcast_id") is None


async def test_the_new_link_closes_the_old_one_and_the_audit_says_it_replaced_one(world: World, key: BotKey) -> None:
    user, _, _, _ = await issued_person(world)
    old = await issue_link(world.session)
    await config_broadcast(world, user)

    (item,) = (await claim(world.session, key)).items

    with pytest.raises(NotFoundError):
        await open_link(world.session, old.token)
    assert (await open_link(world.session, token_of(item.config_url))).config
    assert sorted(bool(event.meta["replaced"]) for event in await events(AUDIT_LINK)) == [False, True]


async def test_a_second_handout_after_a_lost_answer_issues_another_link_and_the_first_dies(
    world: World, key: BotKey
) -> None:
    user, _, _, _ = await issued_person(world)
    await config_broadcast(world, user)
    (first,) = (await claim(world.session, key)).items
    await expire_lease()  # ответ claim потерялся, подтверждения не было

    (second,) = (await claim(world.session, key)).items

    assert token_of(first.config_url) != token_of(second.config_url)
    with pytest.raises(NotFoundError):
        await open_link(world.session, token_of(first.config_url))
    assert (await open_link(world.session, token_of(second.config_url))).config
    assert len(await live_links_in_db()) == 1
    assert (await outbox_rows())[0].config_link_id == (await live_links_in_db())[0].id


async def test_access_revoked_between_queueing_and_the_claim_skips_the_row_without_a_link(
    world: World, key: BotKey
) -> None:
    user, _, grant, _ = await issued_person(world)
    broadcast = await config_broadcast(world, user)
    await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(grant.id)

    claimed = await claim(world.session, key)

    assert list(claimed.items) == [] and await links_in_db() == []
    (row,) = await outbox_rows()
    assert status_value(row.status) == SKIPPED and row.error == NO_ACCESS and row.config_link_id is None
    assert counters(await broadcast_row(broadcast.id))["skipped"] == 1
    await assert_counters_consistent(broadcast.id)


async def test_a_person_with_only_imported_clients_is_skipped_as_config_unavailable(world: World, key: BotKey) -> None:
    user, _, _, credential = await issued_person(world)
    credential.source = CredentialSource.IMPORTED
    credential.encrypted_config = None
    await world.session.commit()
    await config_broadcast(world, user)

    claimed = await claim(world.session, key)

    assert list(claimed.items) == []
    assert (await outbox_rows())[0].error == CONFIG_UNAVAILABLE and await links_in_db() == []


async def test_the_panel_address_removed_before_the_claim_skips_the_row_as_not_configured(
    world: World, key: BotKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _, _, _ = await issued_person(world)
    await config_broadcast(world, user)
    set_public_address(monkeypatch, "", domain="")

    claimed = await claim(world.session, key)

    assert list(claimed.items) == [] and await links_in_db() == []
    (row,) = await outbox_rows()
    assert status_value(row.status) == SKIPPED and row.error == NOT_CONFIGURED


async def test_a_client_still_being_issued_puts_the_row_back_for_a_minute(world: World, key: BotKey) -> None:
    user = await schedule_issue_in_progress(world)
    broadcast = await config_broadcast(world, user)

    claimed = await claim(world.session, key)

    assert list(claimed.items) == [] and await links_in_db() == []
    (row,) = await outbox_rows()
    assert status_value(row.status) == PENDING and row.lease_id is None
    assert in_a_while(seconds=50) <= row.not_before <= in_a_while(seconds=70)
    assert counters(await broadcast_row(broadcast.id))["skipped"] == 0  # это ещё не итог


async def test_a_row_that_waited_for_the_client_too_long_is_skipped_as_not_ready(world: World, key: BotKey) -> None:
    user = await schedule_issue_in_progress(world)
    broadcast = await config_broadcast(world, user)
    await waited(NOT_READY_GIVE_UP_MINUTES + 1)

    claimed = await claim(world.session, key)

    assert list(claimed.items) == []
    (row,) = await outbox_rows()
    assert status_value(row.status) == SKIPPED and row.error == NOT_READY
    assert counters(await broadcast_row(broadcast.id))["skipped"] == 1


async def test_a_row_that_waited_only_a_little_stays_pending_while_the_client_is_issued(
    world: World, key: BotKey
) -> None:
    user = await schedule_issue_in_progress(world)
    await config_broadcast(world, user)
    await waited(NOT_READY_GIVE_UP_MINUTES - 5)

    await claim(world.session, key)

    assert statuses(await outbox_rows()) == [PENDING]


async def test_one_refused_row_does_not_hold_back_the_others_in_the_same_claim(world: World, key: BotKey) -> None:
    good, _, _, _ = await issued_person(world, 5550000001)
    revoked, _, grant, _ = await issued_person(world, 5550000002)
    await AccessGrantService(world.session, SYSTEM_ACTOR).revoke(grant.id)
    await config_broadcast(world, good, revoked)

    claimed = await claim(world.session, key)

    assert [item.telegram_id for item in claimed.items] == [good.telegram_id]
    assert sorted(statuses(await outbox_rows())) == [CLAIMED, SKIPPED]


async def test_a_protocol_broadcast_issues_the_link_on_the_client_of_that_protocol(world: World, key: BotKey) -> None:
    _, first_protocol, _ = await world.server()
    _, second_protocol, _ = await world.server()
    assert first_protocol is not None and second_protocol is not None
    _, subscription, _, first_credential = await issued_person(world, TELEGRAM_ID, first_protocol)
    second_grant = await AccessGrantService(world.session, SYSTEM_ACTOR).create(
        subscription.id, AccessGrantCreate(protocol_id=second_protocol.id)
    )
    second_credential = await world.credentials().provision(second_grant.id)  # новее первого

    await create_broadcast(world.session, kind=CONFIG, target=TARGET_PROTOCOL, protocol_id=first_protocol.id)
    await claim(world.session, key)

    (link,) = await live_links_in_db()
    assert link.credential_id == first_credential.id != second_credential.id


# --- ссылка одному человеку ----------------------------------------------------------------------------------------


async def test_an_individual_link_is_queued_without_a_broadcast_with_priority_zero(world: World) -> None:
    user, _, _, _ = await issued_person(world)

    row = await queue_link(world.session, user.id)

    stored = await outbox_row(row.id)
    assert (status_value(stored.kind), stored.broadcast_id, stored.user_id) == (CONFIG, None, user.id)
    assert status_value(stored.status) == PENDING and stored.priority == PRIORITY_INDIVIDUAL
    assert await links_in_db() == []  # ссылка выпускается при выдаче, не при постановке


async def test_asking_again_returns_the_open_row_instead_of_a_second_one(world: World, key: BotKey) -> None:
    user, _, _, _ = await issued_person(world)
    first = await queue_link(world.session, user.id)

    assert (await queue_link(world.session, user.id)).id == first.id
    await claim(world.session, key)
    assert (await queue_link(world.session, user.id)).id == first.id  # выдана, но не подтверждена - тоже открыта
    assert len(await outbox_rows()) == 1


async def test_after_the_link_was_delivered_a_new_one_can_be_queued(world: World, key: BotKey) -> None:
    user, _, _, _ = await issued_person(world)
    first = await queue_link(world.session, user.id)
    await ack_all(world.session, await claim(world.session, key))

    second = await queue_link(world.session, user.id)

    assert second.id != first.id and len(await outbox_rows()) == 2


async def test_an_open_broadcast_row_of_the_same_person_is_returned_for_an_individual_link(world: World) -> None:
    user, _, _, _ = await issued_person(world)
    broadcast = await config_broadcast(world, user)

    row = await queue_link(world.session, user.id)

    assert row.broadcast_id == broadcast.id and len(await outbox_rows()) == 1


async def test_queueing_an_individual_link_writes_one_audit_event_with_only_the_row_id(world: World) -> None:
    user, _, _, _ = await issued_person(world)

    row = await queue_link(world.session, user.id)
    await queue_link(world.session, user.id)

    (event,) = await events(AUDIT_QUEUED)
    assert event.entity_type == "vpn_user" and event.entity_id == user.id
    assert event.meta == {"outbox_id": str(row.id)}
    assert str(user.telegram_id) not in await audit_dump()


async def test_an_individual_link_is_refused_without_a_telegram(world: World) -> None:
    user, _, _, _ = await issued_person(world, None)

    assert await refusal(queue_link(world.session, user.id)) == NO_TELEGRAM
    assert await outbox_rows() == []


async def test_an_individual_link_is_refused_for_a_person_who_blocked_the_bot(world: World) -> None:
    user, _, _, _ = await issued_person(world)
    await sql("UPDATE vpn_users SET bot_blocked_at = now() WHERE id = :id", id=user.id)

    assert await refusal(queue_link(world.session, user.id)) == BOT_BLOCKED
    assert await outbox_rows() == []


async def test_an_individual_link_is_refused_while_the_panel_address_is_not_set(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _, _, _ = await issued_person(world)
    set_public_address(monkeypatch, "", domain="")

    assert await refusal(queue_link(world.session, user.id)) == NOT_CONFIGURED
    assert await outbox_rows() == []


async def test_an_individual_link_is_refused_for_a_person_without_access_before_anything_is_queued(
    world: World,
) -> None:
    user = await add_person(world.session)

    assert await refusal(queue_link(world.session, user.id)) == NO_ACCESS
    assert await outbox_rows() == [] and await links_in_db() == []


async def test_an_individual_link_is_refused_for_imported_clients(world: World) -> None:
    user, _, _, credential = await issued_person(world)
    credential.source = CredentialSource.IMPORTED
    credential.encrypted_config = None
    await world.session.commit()

    assert await refusal(queue_link(world.session, user.id)) == CONFIG_UNAVAILABLE
    assert await outbox_rows() == [] and await links_in_db() == []


async def test_an_individual_link_for_an_unknown_person_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await queue_link(world.session, uuid4())


# --- stage_issue: выпуск без commit --------------------------------------------------------------------------------


async def stage(world: World, user_id: UUID, grant_id: UUID | None = None, **meta):  # type: ignore[no-untyped-def]
    from modules.bot.config_links import ConfigLinkService

    return await ConfigLinkService(world.session, SYSTEM_ACTOR).stage_issue(
        user_id, grant_id, source="outbox", meta=meta
    )


async def test_stage_issue_gives_a_link_and_a_token_but_does_not_commit(world: World) -> None:
    user, _, _, credential = await issued_person(world)

    link, token = await stage(world, user.id)

    assert TOKEN_SHAPE.fullmatch(token) and link.credential_id == credential.id
    assert await links_in_db() == []  # другая сессия ничего не видит: транзакция открыта
    await world.session.commit()
    assert [row.token_hash for row in await live_links_in_db()] == [hash_token(token)]


async def test_a_rolled_back_stage_leaves_neither_a_link_nor_an_audit_event(world: World) -> None:
    user, _, _, _ = await issued_person(world)
    await stage(world, user.id)

    await world.session.rollback()

    assert await links_in_db() == [] and await events(AUDIT_LINK) == []


async def test_stage_issue_audits_the_source_and_passes_the_extra_meta(world: World) -> None:
    user, _, _, _ = await issued_person(world)
    marker = uuid4()

    await stage(world, user.id, broadcast_id=marker)
    await world.session.commit()

    (event,) = await events(AUDIT_LINK)
    assert event.meta["source"] == "outbox" and event.meta["broadcast_id"] == str(marker)


async def test_stage_issue_with_a_grant_takes_the_client_of_that_grant(world: World) -> None:
    user, subscription, first_grant, first_credential = await issued_person(world)
    _, second_protocol, _ = await world.server()
    assert second_protocol is not None
    second_grant = await AccessGrantService(world.session, SYSTEM_ACTOR).create(
        subscription.id, AccessGrantCreate(protocol_id=second_protocol.id)
    )
    second_credential = await world.credentials().provision(second_grant.id)

    link, _ = await stage(world, user.id, first_grant.id)
    assert link.credential_id == first_credential.id
    await world.session.commit()
    link, _ = await stage(world, user.id, second_grant.id)

    assert link.credential_id == second_credential.id
    await world.session.commit()
    assert len(await live_links_in_db()) == 2  # живая ссылка одна на клиента, у двух клиентов - по одной


async def test_stage_issue_refuses_like_the_bot_request_does(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    lonely = await add_person(world.session)

    assert await refusal(stage(world, lonely.id)) == NO_ACCESS
    user, _, _, _ = await issued_person(world, SECOND_TELEGRAM_ID)
    set_public_address(monkeypatch, "", domain="")
    assert await refusal(stage(world, user.id)) == NOT_CONFIGURED


async def test_a_bot_request_for_a_link_still_commits_and_audits_as_a_bot_request(world: World) -> None:
    await issued_person(world)

    await issue_link(world.session)

    assert len(await live_links_in_db()) == 1
    (event,) = await events(AUDIT_LINK)
    assert event.meta["source"] == "bot"


# --- снятие пометки «заблокировал бота» ----------------------------------------------------------------------------


async def test_a_person_who_writes_to_the_bot_is_unmarked_by_the_status_request(world: World) -> None:
    user = await add_person(world.session, blocked=True)

    await registration_service(world.session).by_telegram(TELEGRAM_ID)

    assert (await user_row(user.id)).bot_blocked_at is None


async def test_a_person_who_asks_for_a_link_is_unmarked_by_the_link_request(world: World) -> None:
    user, _, _, _ = await issued_person(world)
    await sql("UPDATE vpn_users SET bot_blocked_at = now() WHERE id = :id", id=user.id)

    await issue_link(world.session)

    assert (await user_row(user.id)).bot_blocked_at is None


async def test_the_status_request_of_an_unmarked_person_changes_nothing(world: World) -> None:
    user = await add_person(world.session)
    before = (await user_row(user.id)).updated_at

    await registration_service(world.session).by_telegram(TELEGRAM_ID)
    await registration_service(world.session).by_telegram(TELEGRAM_ID)

    after = await user_row(user.id)
    assert after.bot_blocked_at is None and after.updated_at == before
    assert "bot_" not in await audit_dump()  # пометка - не действие админа: в аудит не пишется


async def test_unmarking_is_not_an_audit_event_either(world: World) -> None:
    await add_person(world.session, blocked=True)

    await registration_service(world.session).by_telegram(TELEGRAM_ID)

    assert await audit_dump() == ""


async def test_the_status_request_of_an_unknown_person_is_still_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await registration_service(world.session).by_telegram(5559990001)


async def test_an_unmarked_person_is_in_the_next_broadcast_again(world: World) -> None:
    await add_person(world.session, blocked=True)
    assert counters(await broadcast_row((await create_broadcast(world.session)).id))["total"] == 0

    await registration_service(world.session).by_telegram(TELEGRAM_ID)
    second = await create_broadcast(world.session)

    assert counters(await broadcast_row(second.id))["total"] == 1


# --- хранение ------------------------------------------------------------------------------------------------------


async def test_the_retention_rule_removes_old_finished_rows_and_keeps_the_totals_of_the_broadcast(
    world: World, key: BotKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infrastructure.workers.scheduler import build_rules

    _, broadcast = await news_for(world, 5)
    await ack_all(world.session, await claim(world.session, key, 3))  # 3 отправлено, 2 ждут
    await age_rows(60)
    (fresh_id,) = (await sql_rows("SELECT id FROM bot_outbox WHERE status = 'sent' ORDER BY id LIMIT 1"))[0]
    await sql(
        "UPDATE bot_outbox SET created_at = now(), updated_at = now(), sent_at = now() WHERE id = :id", id=fresh_id
    )
    monkeypatch.setattr(settings, "bot_outbox_retention_days", 30)
    before = counters(await broadcast_row(broadcast.id))
    (retention,) = [rule for rule in build_rules(settings) if rule.name == "retention"]

    await retention.action()

    rows = await outbox_rows()
    assert sorted(statuses(rows)) == [PENDING, PENDING, SENT]  # старые завершённые ушли, открытые и свежая остались
    assert counters(await broadcast_row(broadcast.id)) == before  # итоги рассылки переживают чистку (Р4-A)
    assert await broadcast_row(broadcast.id) is not None


async def test_the_retention_rule_keeps_old_open_rows(
    world: World, key: BotKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infrastructure.workers.scheduler import build_rules

    await news_for(world, 2)
    await claim(world.session, key, 1)
    await age_rows(90)
    monkeypatch.setattr(settings, "bot_outbox_retention_days", 30)
    (retention,) = [rule for rule in build_rules(settings) if rule.name == "retention"]

    await retention.action()

    assert sorted(statuses(await outbox_rows())) == [CLAIMED, PENDING]
