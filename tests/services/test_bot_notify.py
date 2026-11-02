"""Сборщик уведомлений `bot_notify` (этап 25.5, Р1-Р4, Р8-Р9): события аудита -> строки `notify` в `bot_outbox`.

Главное:
- событие, чей `created_at` раньше курсора (транзакция началась раньше, закоммичена позже), доставляется следующим
  проходом - из-за перекрытия `BOT_NOTIFY_OVERLAP_SECONDS` и таблицы `bot_notify_seen`;
- каждое событие обрабатывается один раз; нет привязок - двигается только курсор;
- свёртка за проход: больше 3 событий одного ключа одному админу - одно сообщение;
- в тексте только имя сервера, вид сбоя и время - ни адреса, ни почты, ни `telegram_id`, ни текста ошибки.

Записи аудита вставляются напрямую с заданным `created_at` (`tests/bot_notify_support.py`)."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from core.config import settings
from core.database.postgres import session_factory
from modules.audit.models.audit_log import AuditAction
from tests.bot_notify_support import (
    BIDI,
    GATEWAY_ALL_DOWN,
    HEALTH_CHANGED,
    JOBS_FAILED,
    LOGIN_LOCKED,
    SERVER_DOWN,
    SERVER_RECOVERED,
    TG_ADMIN,
    TG_ADMIN_2,
    add_audit,
    add_health_event,
    admin_login,
    collect,
    cursor,
    deactivate_admin,
    default_events,
    make_link,
    minutes_ago,
    notify_rows,
    seen_ids,
    set_cursor,
)
from tests.bot_support import set_public_address, sql, sql_rows
from tests.fixtures import World

OVERLAP = timedelta(seconds=2100)


async def scene(world: World, **link: Any) -> tuple[UUID, UUID, Any]:
    """Админ с живой привязкой и сервер; курсор стоит чуть раньше «сейчас», чтобы события последних минут читались."""
    admin = await admin_login()
    link_id = await make_link(admin.admin.id, **link)
    server, _, _ = await world.server()
    await set_cursor(minutes_ago(10))
    return admin.admin.id, link_id, server


async def texts() -> list[str]:
    return [str(row.text) for row in await notify_rows()]


# --- адресаты ---------------------------------------------------------------------------------------------------------


async def test_a_server_going_down_queues_one_plain_message_for_the_linked_admin(world: World) -> None:
    admin_id, link_id, server = await scene(world)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    (row,) = await notify_rows()
    assert (row.kind, row.format, row.notify_key) == ("notify", "plain", SERVER_DOWN)
    assert row.admin_link_id == link_id and row.user_id is None and row.status == "pending"
    assert server.name in row.text


async def test_recovery_is_a_separate_event_with_its_own_key(world: World) -> None:
    _, _, server = await scene(world)
    await add_health_event(server.id, "down", "ok", at=minutes_ago(1))

    await collect()

    (row,) = await notify_rows()
    assert row.notify_key == SERVER_RECOVERED and server.name in row.text


async def test_changes_that_are_not_in_the_catalog_queue_nothing_but_are_marked_seen(world: World) -> None:
    _, _, server = await scene(world)
    first = await add_health_event(server.id, "ok", "degraded", at=minutes_ago(2))
    second = await add_health_event(server.id, None, "ok", at=minutes_ago(1))

    await collect()

    assert await notify_rows() == []
    assert {first, second} <= await seen_ids()


async def test_an_admin_without_a_telegram_gets_nothing(world: World) -> None:
    admin = await admin_login()
    await make_link(admin.admin.id, telegram_id=None)  # код выпущен, Telegram не привязан
    server, _, _ = await world.server()
    await set_cursor(minutes_ago(10))
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    assert await notify_rows() == []


async def test_a_blocked_link_gets_nothing(world: World) -> None:
    _, _, server = await scene(world, blocked=True)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    assert await notify_rows() == []


async def test_an_inactive_admin_gets_nothing(world: World) -> None:
    admin_id, _, server = await scene(world)
    await deactivate_admin(admin_id)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    assert await notify_rows() == []


async def test_an_admin_gets_only_the_events_he_chose(world: World) -> None:
    events = {**default_events(), SERVER_DOWN: False}
    _, _, server = await scene(world, events=events)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(2))
    await add_audit("gateway_all_exits_down", entity_id=server.id, at=minutes_ago(1))

    await collect()

    assert [row.notify_key for row in await notify_rows()] == [GATEWAY_ALL_DOWN]


async def test_login_locked_is_off_by_default_and_works_when_switched_on(world: World) -> None:
    admin_id, _, server = await scene(world)
    await add_audit("login_locked", entity_type="admin", entity_id=admin_id, at=minutes_ago(1))
    await collect()
    assert await notify_rows() == []

    other = await admin_login()
    await make_link(other.admin.id, TG_ADMIN_2, events={**default_events(), LOGIN_LOCKED: True})
    await add_audit("login_locked", entity_type="admin", entity_id=admin_id, at=minutes_ago(0.5))
    await collect()

    (row,) = await notify_rows()
    assert row.notify_key == LOGIN_LOCKED


async def test_every_admin_gets_his_own_message(world: World) -> None:
    _, first_link, server = await scene(world)
    other = await admin_login()
    second_link = await make_link(other.admin.id, TG_ADMIN_2)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    assert {row.admin_link_id for row in await notify_rows()} == {first_link, second_link}


# --- каждое событие один раз -----------------------------------------------------------------------------------------


async def test_an_event_is_delivered_once_however_many_passes_run(world: World) -> None:
    _, _, server = await scene(world)
    audit_id = await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()
    await collect()
    await collect()

    assert len(await notify_rows()) == 1 and audit_id in await seen_ids()


async def test_the_cursor_moves_to_the_newest_event_read(world: World) -> None:
    _, _, server = await scene(world)
    newest = minutes_ago(1)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(3))
    await add_health_event(server.id, "down", "ok", at=newest)

    await collect()

    stored = await cursor()
    assert stored is not None and abs(stored - newest) < timedelta(seconds=1)


async def test_an_event_committed_after_the_cursor_passed_it_is_still_delivered(world: World) -> None:
    """Главный тест Р1: транзакция события началась раньше (created_at = её now()), а закоммичена позже, когда курсор
    уже ушёл вперёд по более новому событию. Следующий проход должен её подобрать, а новое - не продублировать."""
    _, _, server = await scene(world)
    late_at, newer_at = minutes_ago(5), minutes_ago(1)

    late = session_factory()
    session = await late.__aenter__()
    try:
        await session.execute(
            text(
                "INSERT INTO audit_logs (id, action, entity_type, entity_id, metadata, created_at) "
                "VALUES (:id, :action, 'server', :eid, CAST(:meta AS jsonb), :at)"
            ),
            {
                "id": uuid4(),
                "action": AuditAction(HEALTH_CHANGED).name,
                "eid": server.id,
                "meta": json.dumps({"from": "down", "to": "ok"}),
                "at": late_at,
            },
        )  # открыта, не закоммичена
        await add_health_event(server.id, "ok", "down", reason="unreachable", at=newer_at)

        await collect()
        first_pass = [row.notify_key for row in await notify_rows()]
        stored = await cursor()

        await session.commit()  # поздний commit
    finally:
        await late.__aexit__(None, None, None)

    assert first_pass == [SERVER_DOWN]
    assert stored is not None and stored > late_at  # курсор действительно ушёл дальше позднего события

    await collect()

    assert sorted(row.notify_key for row in await notify_rows()) == [SERVER_DOWN, SERVER_RECOVERED]


async def test_an_event_older_than_the_overlap_before_the_cursor_is_not_picked_up(world: World) -> None:
    _, _, server = await scene(world)
    await set_cursor(minutes_ago(1))
    await add_health_event(
        server.id, "ok", "down", reason="unreachable", at=datetime.now(timezone.utc) - OVERLAP - timedelta(minutes=5)
    )

    await collect()

    assert await notify_rows() == []


async def test_there_is_no_history_before_the_first_run(world: World) -> None:
    admin = await admin_login()
    await make_link(admin.admin.id)
    server, _, _ = await world.server()
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(180))
    assert await cursor() is None  # курсор ставит миграция; после TRUNCATE строки нет

    await collect()

    assert await notify_rows() == []
    stored = await cursor()
    assert stored is not None and datetime.now(timezone.utc) - stored < timedelta(minutes=1)


async def test_without_links_only_the_cursor_and_seen_move(world: World) -> None:
    server, _, _ = await world.server()
    await set_cursor(minutes_ago(10))
    audit_id = await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(2))

    await collect()

    assert await notify_rows() == [] and audit_id in await seen_ids()
    admin = await admin_login()
    await make_link(admin.admin.id)
    await collect()
    assert await notify_rows() == []  # прошлое не доставляется задним числом


async def test_batch_limit_spreads_events_over_passes_without_loss_or_repeats(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "bot_notify_batch", 3)
    _, _, server = await scene(world)
    for index in range(5):
        await add_audit("server_sync_failed", entity_id=server.id, at=minutes_ago(5 - index))

    await collect()
    assert len(await notify_rows()) == 3
    await collect()
    assert len(await notify_rows()) == 5
    await collect()
    assert len(await notify_rows()) == 5


async def test_old_seen_rows_are_cleaned_and_fresh_ones_stay(world: World) -> None:
    await scene(world)
    old, fresh = uuid4(), uuid4()
    await sql("INSERT INTO bot_notify_seen (audit_log_id, seen_at) VALUES (:id, now() - interval '3 hours')", id=old)
    await sql("INSERT INTO bot_notify_seen (audit_log_id, seen_at) VALUES (:id, now() - interval '1 minute')", id=fresh)

    await collect()

    remaining = await seen_ids()
    assert old not in remaining and fresh in remaining


# --- свёртка ---------------------------------------------------------------------------------------------------------


async def test_three_failures_of_one_kind_stay_separate(world: World) -> None:
    _, _, server = await scene(world)
    for index in range(3):
        await add_audit("server_sync_failed", entity_id=server.id, at=minutes_ago(3 - index * 0.5))

    await collect()

    assert [row.notify_key for row in await notify_rows()] == [JOBS_FAILED] * 3


async def test_more_than_three_failures_collapse_into_one_message_with_a_tail(world: World) -> None:
    _, _, server = await scene(world)
    for index in range(12):
        await add_audit("server_install_failed", entity_id=server.id, at=minutes_ago(9 - index * 0.5))

    await collect()

    (row,) = await notify_rows()
    assert row.notify_key == JOBS_FAILED and "12" in row.text
    assert row.text.count(server.name) <= 5  # до пяти строк «сервер - действие»
    assert "7" in row.text  # «и ещё 7, см. журнал»
    assert len(row.text) <= 4096


async def test_collapse_is_per_admin_and_per_key(world: World) -> None:
    _, _, server = await scene(world)
    for index in range(5):
        await add_audit("server_sync_failed", entity_id=server.id, at=minutes_ago(9 - index))
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    assert sorted(row.notify_key for row in await notify_rows()) == [JOBS_FAILED, SERVER_DOWN]


# --- срок и текст -----------------------------------------------------------------------------------------------------


async def test_the_message_expires_a_day_after_the_event(world: World) -> None:
    _, _, server = await scene(world)
    at = minutes_ago(3)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=at)

    await collect()

    (row,) = await notify_rows()
    assert abs(row.expires_at - (at + timedelta(hours=24))) < timedelta(seconds=2)


async def test_the_ttl_comes_from_settings(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_notify_ttl_hours", 2)
    _, _, server = await scene(world)
    at = minutes_ago(3)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=at)

    await collect()

    (row,) = await notify_rows()
    assert abs(row.expires_at - (at + timedelta(hours=2))) < timedelta(seconds=2)


@pytest.mark.parametrize("zone", ["UTC", "Europe/Moscow", "Asia/Tokyo"])
async def test_the_event_time_is_shown_in_the_configured_timezone(
    world: World, monkeypatch: pytest.MonkeyPatch, zone: str
) -> None:
    monkeypatch.setattr(settings, "bot_notify_timezone", zone)
    _, _, server = await scene(world)
    at = minutes_ago(4)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=at)

    await collect()

    (row,) = await notify_rows()
    assert at.astimezone(ZoneInfo(zone)).strftime("%H:%M") in row.text


async def test_the_reason_changes_the_text(world: World) -> None:
    _, _, server = await scene(world)
    other_server, _, _ = await world.server()
    name, other_name = "Север", "Юг"
    await sql("UPDATE vpn_servers SET name = :name WHERE id = :id", name=name, id=server.id)
    await sql("UPDATE vpn_servers SET name = :name WHERE id = :id", name=other_name, id=other_server.id)
    at = minutes_ago(2)  # одно и то же время: текст может отличаться только видом сбоя
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=at)
    await add_health_event(other_server.id, "ok", "down", reason="ssh_error", at=at)

    await collect()

    bodies = [row.text for row in await notify_rows()]
    unreachable = next(body for body in bodies if name in body)
    ssh_error = next(body for body in bodies if other_name in body)
    assert "нет связи" in unreachable and "ошибка SSH" not in unreachable
    assert "ошибка SSH" in ssh_error and "нет связи" not in ssh_error


async def test_control_and_bidi_characters_are_cut_from_names(world: World) -> None:
    _, _, server = await scene(world)
    await sql("UPDATE vpn_servers SET name = :name WHERE id = :id", name=f"A{BIDI}B", id=server.id)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    (row,) = await notify_rows()
    assert "AB" in row.text and not any(char in row.text for char in BIDI)


async def test_a_deleted_server_is_shown_by_a_short_id(world: World) -> None:
    await scene(world)
    gone = uuid4()
    await add_health_event(gone, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    (row,) = await notify_rows()
    assert gone.hex[:8] in row.text.replace("-", "")


async def test_the_text_holds_no_address_email_telegram_id_or_error_text(world: World) -> None:
    _, _, server = await scene(world)
    await sql("UPDATE vpn_servers SET host = '203.0.113.77' WHERE id = :id", id=server.id)
    await add_audit(
        "server_install_failed",
        entity_id=server.id,
        meta={"error": "boom password=Hunter2 root@203.0.113.77", "email": "ivan@example.org"},
        at=minutes_ago(2),
    )
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    for body in await texts():
        assert "203.0.113.77" not in body and "Hunter2" not in body and "@example.org" not in body
        assert str(TG_ADMIN) not in body and len(body) <= 4096


async def test_the_text_ends_with_a_link_to_the_audit_log_when_the_panel_address_is_known(world: World) -> None:
    _, _, server = await scene(world)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    (row,) = await notify_rows()
    assert "https://panel.example.test/audit" in row.text


async def test_no_link_without_a_panel_address(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    set_public_address(monkeypatch, "")
    _, _, server = await scene(world)
    await add_health_event(server.id, "ok", "down", reason="unreachable", at=minutes_ago(1))

    await collect()

    (row,) = await notify_rows()
    assert "/audit" not in row.text


async def test_panel_down_is_never_produced_by_the_collector(world: World) -> None:
    await scene(world)
    await collect()
    assert [row[0] for row in await sql_rows("SELECT notify_key FROM bot_outbox")] == []


async def test_initial_floor_excludes_recent_history_on_later_passes(world: World) -> None:
    admin = await admin_login()
    await make_link(admin.admin.id)
    server, _, _ = await world.server()
    floor = minutes_ago(2)
    await sql("INSERT INTO bot_notify_cursor (id, cursor_at, started_at) VALUES (1, :at, :at)", at=floor)
    await add_health_event(server.id, "ok", "down", at=floor - timedelta(minutes=1))
    await add_health_event(server.id, "down", "ok", at=floor + timedelta(minutes=1))
    await collect()
    await collect()
    assert [row.notify_key for row in await notify_rows()] == [SERVER_RECOVERED]


async def test_cursor_initialized_without_migration_never_imports_recent_history(world: World) -> None:
    admin = await admin_login()
    await make_link(admin.admin.id)
    server, _, _ = await world.server()
    await add_health_event(server.id, "ok", "down", at=minutes_ago(1))
    await collect()
    await collect()
    assert await notify_rows() == []


async def test_expired_event_does_not_swallow_three_fresh_events(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_notify_ttl_hours", 1)
    _, _, server = await scene(world)
    await set_cursor(minutes_ago(120))
    expired = await add_audit("server_sync_failed", entity_id=server.id, at=minutes_ago(90))
    for minute in (3, 2, 1):
        await add_audit("server_sync_failed", entity_id=server.id, at=minutes_ago(minute))
    await collect()
    rows = await notify_rows()
    assert len(rows) == 3 and all(row.expires_at > datetime.now(timezone.utc) for row in rows)
    assert expired in await seen_ids()
