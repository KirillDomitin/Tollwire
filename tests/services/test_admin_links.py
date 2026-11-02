"""Привязка Telegram к админу (этап 25.5, Р4, Р5, Р11): `modules.bot.admin_links.AdminLinkService`.

- код: 32 символа, в БД SHA-256, TTL из настроек, одноразовый, новый гасит старый;
- привязка: успех, просрочен, использован, `telegram_taken`, замена своего Telegram, сброс блокировки;
- события: выбираются по каталогу; отвязка идемпотентна и уносит очередь; чаты для heartbeat;
- аудит без кода, `telegram_id`, имени пользователя Telegram."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from core.config import settings
from core.exceptions import InvalidDataError, ReasonConflictError
from core.security import hash_token
from modules.audit.actor import AuditActor
from tests.bot_notify_support import (
    LINK_CODE_LENGTH,
    LINK_EXPIRED,
    LINK_INVALID,
    PANEL_DOWN,
    SERVER_DOWN,
    TELEGRAM_TAKEN,
    TG_ADMIN,
    TG_ADMIN_2,
    audit_rows,
    bind,
    deactivate_admin,
    default_events,
    issue_code,
    link_of,
    link_rows,
    link_service,
    make_link,
    notify_rows,
    queue_notify,
)
from tests.bot_support import sql, sql_rows, tables_dump
from tests.integration.conftest import make_admin


async def reason_of(coro) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(ReasonConflictError) as caught:
        await coro
    return str(caught.value.reason)


# --- код ------------------------------------------------------------------------------------------------------------


async def test_the_code_is_32_url_safe_characters(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)

    code, _ = await issue_code(session, admin.admin.id)

    assert len(code) == LINK_CODE_LENGTH and re.fullmatch(r"[A-Za-z0-9_-]+", code)


async def test_codes_are_different_every_time(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)

    codes = {(await issue_code(session, admin.admin.id))[0] for _ in range(5)}

    assert len(codes) == 5


async def test_the_database_keeps_a_hash_of_the_code_and_never_the_code(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)

    code, _ = await issue_code(session, admin.admin.id)

    link = await link_of(admin.admin.id)
    assert link is not None and link["code_hash"] == hash_token(code)
    assert code not in await tables_dump("admin_telegram_links", "audit_logs")


async def test_the_code_lives_for_the_configured_time(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)

    _, expires_at = await issue_code(session, admin.admin.id)

    expected = datetime.now(timezone.utc) + timedelta(seconds=settings.bot_admin_link_code_ttl_seconds)
    assert abs(expires_at - expected) < timedelta(seconds=5)


async def test_the_first_code_creates_an_unlinked_row_with_default_events(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)

    await issue_code(session, admin.admin.id)

    link = await link_of(admin.admin.id)
    assert link is not None and link["telegram_id"] is None and link["linked_at"] is None
    assert link["events"] == default_events()


async def test_a_new_code_kills_the_previous_one(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    old, _ = await issue_code(session, admin.admin.id)
    new, _ = await issue_code(session, admin.admin.id)

    assert await reason_of(bind(session, old)) == LINK_INVALID
    assert (await bind(session, new)).admin_name


async def test_a_new_code_does_not_unlink_a_linked_admin(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    code, _ = await issue_code(session, admin.admin.id)
    await bind(session, code)

    await issue_code(session, admin.admin.id)

    link = await link_of(admin.admin.id)
    assert link is not None and link["telegram_id"] == TG_ADMIN


async def test_a_code_audit_event_has_no_code(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)

    code, _ = await issue_code(session, admin.admin.id, AuditActor(admin_id=admin.admin.id))

    (row,) = await audit_rows("bot_admin_link_code_created")
    assert row[0] == admin.admin.id and row[1] == "admin_telegram_link"
    assert code not in str(row) and hash_token(code) not in str(row)


# --- привязка -------------------------------------------------------------------------------------------------------


async def test_a_valid_code_links_the_telegram(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    code, _ = await issue_code(session, admin.admin.id)

    result = await bind(session, code, TG_ADMIN, "admin_tg")

    assert result.admin_name
    link = await link_of(admin.admin.id)
    assert link is not None
    assert (link["telegram_id"], link["telegram_username"]) == (TG_ADMIN, "admin_tg")
    assert link["linked_at"] is not None and link["blocked_at"] is None


async def test_the_code_works_once(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    code, _ = await issue_code(session, admin.admin.id)
    await bind(session, code, TG_ADMIN)

    assert await reason_of(bind(session, code, TG_ADMIN_2)) == LINK_INVALID

    link = await link_of(admin.admin.id)
    assert link is not None and link["telegram_id"] == TG_ADMIN and link["code_hash"] is None


async def test_an_unknown_code_is_invalid(session) -> None:  # type: ignore[no-untyped-def]
    assert await reason_of(bind(session, "x" * LINK_CODE_LENGTH)) == LINK_INVALID


async def test_an_expired_code_is_reported_as_expired_and_links_nothing(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    code, _ = await issue_code(session, admin.admin.id)
    await sql("UPDATE admin_telegram_links SET code_expires_at = now() - interval '1 second'")

    assert await reason_of(bind(session, code)) == LINK_EXPIRED

    link = await link_of(admin.admin.id)
    assert link is not None and link["telegram_id"] is None


async def test_the_code_of_a_deactivated_admin_is_invalid(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    code, _ = await issue_code(session, admin.admin.id)
    await deactivate_admin(admin.admin.id)

    assert await reason_of(bind(session, code)) == LINK_INVALID


async def test_a_telegram_of_another_admin_is_taken(session) -> None:  # type: ignore[no-untyped-def]
    first = await make_admin(session)
    await make_link(first.admin.id, TG_ADMIN)
    second = await make_admin(session)
    code, _ = await issue_code(session, second.admin.id)

    assert await reason_of(bind(session, code, TG_ADMIN)) == TELEGRAM_TAKEN

    other = await link_of(second.admin.id)
    assert other is not None and other["telegram_id"] is None
    mine = await link_of(first.admin.id)
    assert mine is not None and mine["telegram_id"] == TG_ADMIN


async def test_relinking_replaces_the_telegram_of_the_same_admin_and_keeps_his_events(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    events = {**default_events(), SERVER_DOWN: False}
    await make_link(admin.admin.id, TG_ADMIN, events=events)
    code, _ = await issue_code(session, admin.admin.id)

    await bind(session, code, TG_ADMIN_2, "new_name")

    link = await link_of(admin.admin.id)
    assert link is not None
    assert (link["telegram_id"], link["telegram_username"], link["events"]) == (TG_ADMIN_2, "new_name", events)
    assert len(await link_rows()) == 1


async def test_relinking_the_same_telegram_is_fine(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    await make_link(admin.admin.id, TG_ADMIN)
    code, _ = await issue_code(session, admin.admin.id)

    await bind(session, code, TG_ADMIN)

    link = await link_of(admin.admin.id)
    assert link is not None and link["telegram_id"] == TG_ADMIN


async def test_linking_clears_the_block_flag(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    await make_link(admin.admin.id, TG_ADMIN, blocked=True)
    code, _ = await issue_code(session, admin.admin.id)

    await bind(session, code, TG_ADMIN)

    link = await link_of(admin.admin.id)
    assert link is not None and link["blocked_at"] is None


async def test_the_link_audit_event_names_the_admin_and_holds_no_secrets(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    code, _ = await issue_code(session, admin.admin.id)

    await bind(session, code, TG_ADMIN, "admin_tg")

    (row,) = await audit_rows("bot_admin_link_created")
    assert row[0] is None  # действует бот, не админ
    assert row[3] == {"admin_id": str(admin.admin.id), "replaced": False}
    dump = str(row)
    assert code not in dump and str(TG_ADMIN) not in dump and "admin_tg" not in dump


async def test_replacing_a_telegram_is_marked_in_the_audit(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    await make_link(admin.admin.id, TG_ADMIN)
    code, _ = await issue_code(session, admin.admin.id)

    await bind(session, code, TG_ADMIN_2)

    (row,) = await audit_rows("bot_admin_link_created")
    assert row[3]["replaced"] is True


async def test_a_refused_link_writes_no_audit_event(session) -> None:  # type: ignore[no-untyped-def]
    await reason_of(bind(session, "y" * LINK_CODE_LENGTH))

    assert await audit_rows("bot_admin_link_created") == []


# --- события --------------------------------------------------------------------------------------------------------


async def test_events_are_saved_per_admin(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    await make_link(admin.admin.id)
    chosen = {**default_events(), SERVER_DOWN: False, "login_locked": True}

    await link_service(session).set_events(admin.admin.id, chosen)

    link = await link_of(admin.admin.id)
    assert link is not None and link["events"] == chosen
    (row,) = await audit_rows("bot_admin_link_events_updated")
    assert row[0] is None or row[0] == admin.admin.id


async def test_an_unknown_event_key_is_refused_and_nothing_changes(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    await make_link(admin.admin.id)

    with pytest.raises((InvalidDataError, ValueError)):
        await link_service(session).set_events(admin.admin.id, {**default_events(), "server_exploded": True})

    link = await link_of(admin.admin.id)
    assert link is not None and link["events"] == default_events()


# --- отвязка --------------------------------------------------------------------------------------------------------


async def test_unlink_removes_the_link_and_its_queue(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    link_id = await make_link(admin.admin.id)
    await queue_notify(link_id)

    await link_service(session).unlink(admin.admin.id)

    assert await link_of(admin.admin.id) is None and await notify_rows() == []


async def test_unlink_twice_is_quiet_and_audited_once(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    await make_link(admin.admin.id)

    await link_service(session).unlink(admin.admin.id)
    await link_service(session).unlink(admin.admin.id)

    assert len(await audit_rows("bot_admin_link_removed")) == 1


async def test_unlink_of_an_admin_who_never_linked_is_quiet(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)

    await link_service(session).unlink(admin.admin.id)

    assert await audit_rows("bot_admin_link_removed") == []


async def test_after_unlink_the_old_telegram_is_free_for_another_admin(session) -> None:  # type: ignore[no-untyped-def]
    first, second = await make_admin(session), await make_admin(session)
    await make_link(first.admin.id, TG_ADMIN)
    await link_service(session).unlink(first.admin.id)
    code, _ = await issue_code(session, second.admin.id)

    await bind(session, code, TG_ADMIN)

    link = await link_of(second.admin.id)
    assert link is not None and link["telegram_id"] == TG_ADMIN


async def test_deleting_an_admin_removes_his_link(session) -> None:  # type: ignore[no-untyped-def]
    admin = await make_admin(session)
    await make_link(admin.admin.id)

    await sql("DELETE FROM admins WHERE id = :id", id=admin.admin.id)

    assert await sql_rows("SELECT id FROM admin_telegram_links") == []


# --- чаты для heartbeat ---------------------------------------------------------------------------------------------


async def test_chats_are_the_live_links_of_active_admins_with_panel_down_on(session) -> None:  # type: ignore[no-untyped-def]
    live = await make_admin(session)
    await make_link(live.admin.id, TG_ADMIN)
    muted = await make_admin(session)
    await make_link(muted.admin.id, 6660000011, events={**default_events(), PANEL_DOWN: False})
    blocked = await make_admin(session)
    await make_link(blocked.admin.id, 6660000012, blocked=True)
    unlinked = await make_admin(session)
    await make_link(unlinked.admin.id, None)
    inactive = await make_admin(session)
    await make_link(inactive.admin.id, 6660000013)
    await deactivate_admin(inactive.admin.id)

    assert await link_service(session).chats() == [TG_ADMIN]
