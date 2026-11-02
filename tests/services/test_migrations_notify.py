"""Миграция этапа 25.5 (уведомления админу): `admin_telegram_links`, `bot_notify_cursor`, `bot_notify_seen`, строки
`notify` в `bot_outbox`, `bot_keys.last_seen_local`. Ревизия поверх `c6e2f9a41d37`.

Схема проверяется голым SQL (без ORM-моделей этапа), downgrade — с данными: строки `notify` удаляются до возврата
`user_id NOT NULL`, иначе откат падал бы."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.database.postgres import engine
from tests.conftest import run_alembic

BEFORE_NOTIFY = "c6e2f9a41d37"


async def query(sql: str, **params: object) -> list[tuple[object, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(text(sql), params)).all()]


async def execute(sql: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def refused(sql: str, **params: object) -> str:
    with pytest.raises(IntegrityError) as caught:
        await execute(sql, **params)
    return str(caught.value.orig).lower()


async def alembic(*args: str) -> None:
    await engine.dispose()
    run_alembic(*args)
    await engine.dispose()


async def add_admin(email: str | None = None) -> UUID:
    admin_id = uuid4()
    await execute(
        "INSERT INTO admins (id, email, password_hash, is_active, role, created_at, updated_at) "
        "VALUES (:id, :email, 'x', true, 'SUPERADMIN', now(), now())",
        id=admin_id,
        email=email or f"{admin_id.hex[:8]}@example.org",
    )
    return admin_id


async def add_link(admin_id: UUID, telegram_id: int | None = 6660001234, **extra: object) -> UUID:
    link_id = uuid4()
    linked = telegram_id is not None
    await execute(
        "INSERT INTO admin_telegram_links (id, admin_id, telegram_id, linked_at, events, created_at, updated_at) "
        "VALUES (:id, :admin, :tg, CASE WHEN :linked THEN now() ELSE NULL END, '{}'::jsonb, now(), now())",
        id=link_id,
        admin=admin_id,
        tg=telegram_id,
        linked=linked,
    )
    for column, value in extra.items():
        await execute(f"UPDATE admin_telegram_links SET {column} = :v WHERE id = :id", v=value, id=link_id)  # noqa: S608
    return link_id


async def add_notify(link_id: UUID) -> UUID:
    outbox_id = uuid4()
    await execute(
        "INSERT INTO bot_outbox (id, kind, admin_link_id, text, format, status, priority, created_at, updated_at) "
        "VALUES (:id, 'notify', :link, 'x', 'plain', 'pending', 0, now(), now())",
        id=outbox_id,
        link=link_id,
    )
    return outbox_id


async def add_user() -> UUID:
    user_id = uuid4()
    await execute(
        "INSERT INTO vpn_users (id, username, email) VALUES (:id, 'u', :email)",
        id=user_id,
        email=f"{user_id.hex[:8]}@example.org",
    )
    return user_id


def columns(table: str) -> str:
    return (
        "SELECT column_name, data_type, is_nullable, character_maximum_length FROM information_schema.columns "
        f"WHERE table_name = '{table}' ORDER BY column_name"  # noqa: S608
    )


async def column_map(table: str) -> dict[str, tuple[object, ...]]:
    return {row[0]: row[1:] for row in await query(columns(table))}  # type: ignore[misc]


# --- схема после upgrade -------------------------------------------------------------------------------------------


async def test_admin_links_table_has_the_agreed_columns() -> None:
    found = await column_map("admin_telegram_links")

    assert set(found) == {
        "id",
        "admin_id",
        "telegram_id",
        "telegram_username",
        "linked_at",
        "blocked_at",
        "events",
        "code_hash",
        "code_expires_at",
        "created_at",
        "updated_at",
    }
    assert found["telegram_id"][:2] == ("bigint", "YES") and found["admin_id"][1] == "NO"
    assert found["events"][:2] == ("jsonb", "NO")
    assert found["code_hash"][:3] == ("character varying", "YES", 64)
    assert found["telegram_username"][2] == 64


async def test_outbox_gets_the_notify_columns_and_a_nullable_user() -> None:
    found = await column_map("bot_outbox")

    assert found["user_id"][1] == "YES"
    assert {"admin_link_id", "text", "format", "expires_at", "notify_key"} <= set(found)
    assert found["format"][2] == 8 and found["notify_key"][2] == 32
    assert all(found[name][1] == "YES" for name in ("admin_link_id", "text", "format", "expires_at", "notify_key"))


async def test_the_cursor_and_seen_tables_exist() -> None:
    columns = await column_map("bot_notify_cursor")
    assert set(columns) == {"id", "cursor_at", "started_at"}
    assert columns["started_at"][1] == "NO"
    assert set(await column_map("bot_notify_seen")) == {"audit_log_id", "seen_at"}


async def test_bot_keys_remember_whether_the_bot_is_local() -> None:
    found = await column_map("bot_keys")

    assert found["last_seen_local"][:2] == ("boolean", "YES")


async def test_the_named_constraints_and_the_index_exist() -> None:
    names = {row[0] for row in await query("SELECT conname FROM pg_constraint")}
    indexes = {row[0] for row in await query("SELECT indexname FROM pg_indexes")}

    assert {"ck_bot_outbox_kind", "ck_bot_outbox_recipient"} <= names
    assert "ix_bot_outbox_admin_link" in indexes
    kind = (await query("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_bot_outbox_kind'"))[0][
        0
    ]
    assert "notify" in str(kind)


async def test_the_migration_puts_the_cursor_at_now() -> None:
    """Курсор = момент миграции, истории нет. После TRUNCATE ждём строку после цикла миграций."""
    await alembic("downgrade", BEFORE_NOTIFY)
    await alembic("upgrade", "head")

    rows = await query("SELECT id, cursor_at FROM bot_notify_cursor")
    assert len(rows) == 1 and rows[0][0] == 1
    cursor_at = rows[0][1]
    assert isinstance(cursor_at, datetime) and abs(datetime.now(timezone.utc) - cursor_at) < timedelta(minutes=2)


# --- ограничения admin_telegram_links ------------------------------------------------------------------------------


async def test_one_link_per_admin() -> None:
    admin_id = await add_admin()
    await add_link(admin_id, 1)

    assert "admin_id" in await refused(
        "INSERT INTO admin_telegram_links (id, admin_id, events, created_at, updated_at) "
        "VALUES (:id, :admin, '{}'::jsonb, now(), now())",
        id=uuid4(),
        admin=admin_id,
    )


async def test_one_telegram_belongs_to_one_admin_but_many_admins_may_have_none() -> None:
    first, second, third = await add_admin(), await add_admin(), await add_admin()
    await add_link(first, 6660001234)
    assert "telegram_id" in await refused(
        "INSERT INTO admin_telegram_links (id, admin_id, telegram_id, linked_at, events, created_at, updated_at) "
        "VALUES (:id, :admin, 6660001234, now(), '{}'::jsonb, now(), now())",
        id=uuid4(),
        admin=second,
    )
    await add_link(second, None)
    await add_link(third, None)

    assert await query("SELECT count(*) FROM admin_telegram_links") == [(3,)]


async def test_telegram_and_linked_at_come_together() -> None:
    admin_id = await add_admin()

    message = await refused(
        "INSERT INTO admin_telegram_links (id, admin_id, telegram_id, events, created_at, updated_at) "
        "VALUES (:id, :admin, 5, '{}'::jsonb, now(), now())",
        id=uuid4(),
        admin=admin_id,
    )

    assert "check" in message or "violates" in message


async def test_code_hash_and_its_expiry_come_together() -> None:
    admin_id = await add_admin()
    link_id = await add_link(admin_id, None)

    with pytest.raises(IntegrityError):
        await execute("UPDATE admin_telegram_links SET code_hash = :h WHERE id = :id", h="h" * 64, id=link_id)
    with pytest.raises(IntegrityError):
        await execute(
            "UPDATE admin_telegram_links SET code_expires_at = now() WHERE id = :id",
            id=link_id,
        )


async def test_a_code_hash_is_unique() -> None:
    first, second = await add_admin(), await add_admin()
    for admin_id in (first,):
        link_id = await add_link(admin_id, None)
        await execute(
            "UPDATE admin_telegram_links SET code_hash = :h, code_expires_at = now() WHERE id = :id",
            h="c" * 64,
            id=link_id,
        )
    other = await add_link(second, None)

    with pytest.raises(IntegrityError):
        await execute(
            "UPDATE admin_telegram_links SET code_hash = :h, code_expires_at = now() WHERE id = :id",
            h="c" * 64,
            id=other,
        )


async def test_deleting_an_admin_removes_the_link_and_the_queue() -> None:
    admin_id = await add_admin()
    link_id = await add_link(admin_id)
    await add_notify(link_id)

    await execute("DELETE FROM admins WHERE id = :id", id=admin_id)

    assert await query("SELECT count(*) FROM admin_telegram_links") == [(0,)]
    assert await query("SELECT count(*) FROM bot_outbox") == [(0,)]


async def test_the_cursor_table_holds_only_row_one() -> None:
    await execute("DELETE FROM bot_notify_cursor")
    await execute("INSERT INTO bot_notify_cursor (id, cursor_at) VALUES (1, now())")

    with pytest.raises(IntegrityError):
        await execute("INSERT INTO bot_notify_cursor (id, cursor_at) VALUES (2, now())")


async def test_seen_has_no_foreign_key_to_the_audit_log() -> None:
    """Аудит чистит retention: запись о просмотренном не должна мешать этому и не должна исчезать каскадом."""
    audit_id = uuid4()

    await execute("INSERT INTO bot_notify_seen (audit_log_id, seen_at) VALUES (:id, now())", id=audit_id)

    assert await query("SELECT count(*) FROM bot_notify_seen") == [(1,)]


# --- downgrade ------------------------------------------------------------------------------------------------------


async def test_downgrade_removes_notify_rows_first_and_keeps_the_rest_then_upgrade_restores() -> None:
    admin_id = await add_admin()
    link_id = await add_link(admin_id)
    await add_notify(link_id)
    user_id = await add_user()
    kept = uuid4()
    await execute(
        "INSERT INTO bot_outbox (id, kind, user_id, status, priority, created_at, updated_at) "
        "VALUES (:id, 'config', :user, 'pending', 0, now(), now())",
        id=kept,
        user=user_id,
    )
    await alembic("downgrade", BEFORE_NOTIFY)
    try:
        assert [row[0] for row in await query("SELECT id FROM bot_outbox")] == [kept]
        found = await column_map("bot_outbox")
        assert found["user_id"][1] == "NO"
        assert not {"admin_link_id", "text", "format", "expires_at", "notify_key"} & set(found)
        assert "last_seen_local" not in await column_map("bot_keys")
        for table in ("admin_telegram_links", "bot_notify_cursor", "bot_notify_seen"):
            assert await query(f"SELECT tablename FROM pg_tables WHERE tablename = '{table}'") == []  # noqa: S608
        constraints = {row[0] for row in await query("SELECT conname FROM pg_constraint")}
        assert "ck_bot_outbox_recipient" not in constraints and "ck_bot_outbox_kind" in constraints
        kind = (
            await query("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_bot_outbox_kind'")
        )[0][0]
        assert "notify" not in str(kind)
    finally:
        await alembic("upgrade", "head")

    assert [row[0] for row in await query("SELECT id FROM bot_outbox")] == [kept]
    assert await query("SELECT count(*) FROM admin_telegram_links") == [(0,)]  # привязки при откате теряются
    assert (await column_map("bot_outbox"))["user_id"][1] == "YES"


async def test_the_migration_cycle_is_idempotent_and_leaves_no_orphans() -> None:
    await alembic("downgrade", BEFORE_NOTIFY)
    await alembic("upgrade", "head")
    await alembic("downgrade", BEFORE_NOTIFY)
    try:
        indexes = {row[0] for row in await query("SELECT indexname FROM pg_indexes")}
        assert "ix_bot_outbox_admin_link" not in indexes
        leftovers = await query(
            "SELECT tablename FROM pg_tables WHERE tablename IN "
            "('admin_telegram_links', 'bot_notify_cursor', 'bot_notify_seen')"
        )
        assert leftovers == []
    finally:
        await alembic("upgrade", "head")
    assert len(await query("SELECT tablename FROM pg_tables WHERE tablename LIKE 'bot_notify_%'")) == 2
