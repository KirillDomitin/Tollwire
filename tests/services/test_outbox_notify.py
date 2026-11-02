"""Очередь бота, строки `notify` (этап 25.5, Р2, Р3, Р7): получатель — привязка админа, а не человек.

- `claim` без `kinds` отдаёт только `news` и `config` (бот 0.2.0 строк `notify` не видит);
- просроченная строка и строка без получателя закрываются как `skipped`, текст боту не уходит;
- `ack blocked` по `notify` отмечает привязку админа, а не `vpn_users.bot_blocked_at`;
- CHECK `ck_bot_outbox_recipient`: ровно один получатель, соответствующий `kind`."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from modules.bot.models.key import BotKey
from tests.bot_notify_support import (
    TG_ADMIN,
    admin_login,
    claim_kinds,
    claim_notify,
    deactivate_admin,
    link_of,
    make_link,
    notify_rows,
    queue_notify,
)
from tests.bot_support import (
    BLOCKED,
    SENT,
    SKIPPED,
    ack,
    ack_item,
    add_person,
    create_broadcast,
    create_key,
    outbox_row,
    sql,
    status_value,
    user_row,
)


@pytest.fixture
async def key(session) -> BotKey:  # type: ignore[no-untyped-def]
    created, _ = await create_key(session)
    return created


@pytest.fixture
async def linked():  # type: ignore[no-untyped-def]
    admin = await admin_login()
    return admin.admin.id, await make_link(admin.admin.id)


# --- выдача ---------------------------------------------------------------------------------------------------------


async def test_claim_without_kinds_does_not_hand_out_notify(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    await queue_notify(linked[1])

    result = await claim_kinds(session, key, None)

    assert result.items == []
    (row,) = await notify_rows()
    assert status_value(row.status) == "pending"


async def test_claim_with_news_and_config_only_does_not_hand_out_notify(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    await queue_notify(linked[1])

    assert (await claim_kinds(session, key, ["news", "config"])).items == []


async def test_claim_of_notify_gives_the_text_the_format_and_the_telegram_of_the_admin(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    outbox_id = await queue_notify(linked[1], "Сервер alpha недоступен")

    result = await claim_notify(session, key)

    (item,) = result.items
    assert item.id == outbox_id and item.kind == "notify"
    assert item.telegram_id == TG_ADMIN and item.text == "Сервер alpha недоступен" and item.format == "plain"
    assert status_value((await outbox_row(outbox_id)).status) == "claimed"


async def test_claim_follows_the_telegram_the_admin_has_now(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    await queue_notify(linked[1])
    await sql("UPDATE admin_telegram_links SET telegram_id = 777000111 WHERE id = :id", id=linked[1])

    (item,) = (await claim_notify(session, key)).items

    assert item.telegram_id == 777000111


async def test_notify_goes_before_a_broadcast_in_the_queue(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    await add_person(session, 7_000_000_001)
    await create_broadcast(session)
    outbox_id = await queue_notify(linked[1])

    result = await claim_kinds(session, key, ["news", "notify"])

    assert result.items[0].id == outbox_id and [item.kind for item in result.items] == ["notify", "news"]


async def test_claim_respects_the_limit_for_notify(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    for _ in range(4):
        await queue_notify(linked[1])

    assert len((await claim_notify(session, key, limit=3)).items) == 3


# --- отказ при выдаче -----------------------------------------------------------------------------------------------


async def test_an_expired_row_is_skipped_as_expired_and_not_given_to_the_bot(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    outbox_id = await queue_notify(linked[1], expires_in=timedelta(seconds=-5))

    result = await claim_notify(session, key)

    assert result.items == []
    row = await outbox_row(outbox_id)
    assert (status_value(row.status), row.error) == (SKIPPED, "expired")


async def test_a_row_that_has_not_expired_yet_is_handed_out(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    await queue_notify(linked[1], expires_in=timedelta(minutes=1))

    assert len((await claim_notify(session, key)).items) == 1


async def test_a_link_that_lost_its_telegram_is_no_recipient(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    outbox_id = await queue_notify(linked[1])
    await sql("UPDATE admin_telegram_links SET telegram_id = NULL, linked_at = NULL WHERE id = :id", id=linked[1])

    result = await claim_notify(session, key)

    assert result.items == []
    row = await outbox_row(outbox_id)
    assert (status_value(row.status), row.error) == (SKIPPED, "no_recipient")


async def test_a_blocked_link_is_not_written_to(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    outbox_id = await queue_notify(linked[1])
    await sql("UPDATE admin_telegram_links SET blocked_at = now() WHERE id = :id", id=linked[1])

    result = await claim_notify(session, key)

    assert result.items == []
    row = await outbox_row(outbox_id)
    assert status_value(row.status) == SKIPPED and row.error in ("no_recipient", "blocked")


async def test_an_inactive_admin_is_no_recipient(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    outbox_id = await queue_notify(linked[1])
    await deactivate_admin(linked[0])

    result = await claim_notify(session, key)

    assert result.items == []
    row = await outbox_row(outbox_id)
    assert (status_value(row.status), row.error) == (SKIPPED, "no_recipient")


async def test_one_bad_row_does_not_stop_the_good_ones(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    await queue_notify(linked[1], expires_in=timedelta(seconds=-5))
    good = await queue_notify(linked[1], "живое")

    result = await claim_notify(session, key)

    assert [item.id for item in result.items] == [good]


# --- подтверждение --------------------------------------------------------------------------------------------------


async def test_ack_sent_closes_the_row(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    outbox_id = await queue_notify(linked[1])
    result = await claim_notify(session, key)

    await ack(session, result.lease_id, ack_item(outbox_id, SENT))

    assert status_value((await outbox_row(outbox_id)).status) == SENT


async def test_ack_blocked_marks_the_admin_link_and_not_a_vpn_user(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    person = await add_person(session, TG_ADMIN)  # у человека тот же telegram_id: его флаг трогать нельзя
    outbox_id = await queue_notify(linked[1])
    result = await claim_notify(session, key)

    await ack(session, result.lease_id, ack_item(outbox_id, BLOCKED))

    link = await link_of(linked[0])
    assert link is not None and link["blocked_at"] is not None
    assert (await user_row(person.id)).bot_blocked_at is None
    assert status_value((await outbox_row(outbox_id)).status) == BLOCKED


async def test_ack_blocked_of_a_news_row_still_marks_the_user(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    person = await add_person(session, 7_000_000_002)
    await create_broadcast(session)
    claimed = await claim_kinds(session, key, None)
    (item,) = claimed.items

    await ack(session, claimed.lease_id, ack_item(item.id, BLOCKED))

    assert (await user_row(person.id)).bot_blocked_at is not None
    link = await link_of(linked[0])
    assert link is not None and link["blocked_at"] is None


async def test_ack_retry_returns_the_row_to_the_queue(session, key, linked) -> None:  # type: ignore[no-untyped-def]
    outbox_id = await queue_notify(linked[1])
    result = await claim_notify(session, key)

    await ack(session, result.lease_id, ack_item(outbox_id, "retry", error="network", retry_after=30))

    row = await outbox_row(outbox_id)
    assert status_value(row.status) == "pending" and row.not_before is not None


# --- ограничение получателя -----------------------------------------------------------------------------------------


async def refused(statement: str, **params) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(IntegrityError) as caught:
        await sql(statement, **params)
    return str(caught.value.orig).lower()


async def test_a_notify_row_needs_a_link_a_text_and_a_format(linked) -> None:  # type: ignore[no-untyped-def]
    base = (
        "INSERT INTO bot_outbox (id, kind, {cols}, status, priority, created_at, updated_at) "
        "VALUES (:id, 'notify', {vals}, 'pending', 0, now(), now())"
    )
    for cols, vals in (
        ("text, format", "'x', 'plain'"),  # нет привязки
        ("admin_link_id, format", ":link, 'plain'"),  # нет текста
        ("admin_link_id, text", ":link, 'x'"),  # нет формата
    ):
        message = await refused(base.format(cols=cols, vals=vals), id=uuid4(), link=linked[1])
        assert "ck_bot_outbox_recipient" in message


async def test_a_notify_row_cannot_also_have_a_user(session, linked) -> None:  # type: ignore[no-untyped-def]
    person = await add_person(session, 7_000_000_003)

    message = await refused(
        "INSERT INTO bot_outbox (id, kind, admin_link_id, user_id, text, format, status, priority, created_at, "
        "updated_at) VALUES (:id, 'notify', :link, :user, 'x', 'plain', 'pending', 0, now(), now())",
        id=uuid4(),
        link=linked[1],
        user=person.id,
    )

    assert "ck_bot_outbox_recipient" in message


@pytest.mark.parametrize("kind", ["news", "config"])
async def test_a_person_row_cannot_point_to_an_admin_link(session, linked, kind: str) -> None:  # type: ignore[no-untyped-def]
    person = await add_person(session, 7_000_000_004)

    message = await refused(
        "INSERT INTO bot_outbox (id, kind, admin_link_id, user_id, status, priority, created_at, updated_at) "
        f"VALUES (:id, '{kind}', :link, :user, 'pending', 10, now(), now())",
        id=uuid4(),
        link=linked[1],
        user=person.id,
    )

    assert "ck_bot_outbox_recipient" in message


@pytest.mark.parametrize("kind", ["news", "config"])
async def test_a_person_row_needs_a_user(kind: str) -> None:
    message = await refused(
        "INSERT INTO bot_outbox (id, kind, status, priority, created_at, updated_at) "
        f"VALUES (:id, '{kind}', 'pending', 10, now(), now())",
        id=uuid4(),
    )

    assert "ck_bot_outbox_recipient" in message


async def test_an_unknown_kind_is_refused() -> None:
    message = await refused(
        "INSERT INTO bot_outbox (id, kind, status, priority, created_at, updated_at) "
        "VALUES (:id, 'sms', 'pending', 10, now(), now())",
        id=uuid4(),
    )

    assert "ck_bot_outbox_kind" in message


async def test_deleting_the_admin_link_removes_its_queue(linked) -> None:  # type: ignore[no-untyped-def]
    await queue_notify(linked[1])
    await queue_notify(linked[1])

    await sql("DELETE FROM admin_telegram_links WHERE id = :id", id=linked[1])

    assert await notify_rows() == []


async def test_deleting_the_admin_removes_the_link_and_its_queue(linked) -> None:  # type: ignore[no-untyped-def]
    await queue_notify(linked[1])

    await sql("DELETE FROM admins WHERE id = :id", id=linked[0])

    assert await notify_rows() == [] and await link_of(linked[0]) is None


async def test_the_notify_priority_constant_is_zero() -> None:
    from modules.bot.models.outbox import PRIORITY_NOTIFY, OutboxKind

    assert PRIORITY_NOTIFY == 0 and OutboxKind.NOTIFY.value == "notify"
