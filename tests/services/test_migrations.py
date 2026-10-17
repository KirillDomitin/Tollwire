"""Миграции целиком: откат до нуля и обратно до head.

Ловит ошибки, на которые проект уже наступал: забытый DROP TYPE для enum в downgrade (11.1, 13.2),
безымянный внешний ключ, который downgrade не может удалить (12.3, 14.1).
"""

from uuid import UUID

from sqlalchemy import text

from core.database.postgres import engine, session_factory
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.servers.models.server import VPNServer
from tests.conftest import run_alembic

LEFTOVERS_SQL = text(
    """
    SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    UNION ALL
    SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public' AND t.typtype = 'e'
    """
)


async def test_full_downgrade_and_upgrade_cycle() -> None:
    run_alembic("downgrade", "base")
    async with engine.connect() as conn:
        leftovers = (await conn.execute(LEFTOVERS_SQL)).scalars().all()
    assert leftovers == []  # после отката не осталось ни таблиц, ни enum-типов
    run_alembic("upgrade", "head")
    # Таблицы пересозданы — соединения пула держат подготовленные запросы к старым. Сбрасываем пул.
    await engine.dispose()


# --- ключ панели: server_credentials.{managed_key, public_key, key_fingerprint} и jobtype.INSTALL_SSH_KEY ---

KEY_COLUMNS_SQL = text(
    """
    SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
    FROM information_schema.columns
    WHERE table_name = 'server_credentials' AND column_name IN ('managed_key', 'public_key', 'key_fingerprint')
    """
)
JOB_TYPES_SQL = text(
    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'jobtype'"
)


# Откат миграции ключа панели вместе со всем, что накатано поверх неё (ревизия перед ней: b587ddfe2d19).
BEFORE_SSH_KEY = "b587ddfe2d19"


async def _query(sql: object) -> list[tuple[object, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(sql)).all()]  # type: ignore[call-overload]


async def _alembic(*args: str) -> None:
    await engine.dispose()  # соединения пула держали бы старые типы и таблицы
    run_alembic(*args)
    await engine.dispose()


async def test_ssh_key_migration_adds_columns_and_job_type() -> None:
    columns = {row[0]: row for row in await _query(KEY_COLUMNS_SQL)}
    assert set(columns) == {"managed_key", "public_key", "key_fingerprint"}
    assert columns["managed_key"][1:3] == ("boolean", "NO") and "false" in str(columns["managed_key"][3])
    assert columns["public_key"][2] == "YES" and columns["key_fingerprint"][2] == "YES"
    assert columns["key_fingerprint"][4] == 100
    assert "INSTALL_SSH_KEY" in {row[0] for row in await _query(JOB_TYPES_SQL)}


async def test_ssh_key_migration_downgrade_removes_columns_and_job_type_then_upgrade_restores() -> None:
    await _alembic("downgrade", BEFORE_SSH_KEY)
    try:
        assert await _query(KEY_COLUMNS_SQL) == []
        assert "INSTALL_SSH_KEY" not in {row[0] for row in await _query(JOB_TYPES_SQL)}
    finally:
        await _alembic("upgrade", "head")
    assert len(await _query(KEY_COLUMNS_SQL)) == 3
    assert "INSTALL_SSH_KEY" in {row[0] for row in await _query(JOB_TYPES_SQL)}


async def test_ssh_key_migration_downgrade_deletes_jobs_of_new_type_and_keeps_others() -> None:
    """Значение enum нельзя убрать, пока есть строки с ним: откат сначала удаляет такие задачи, остальные целы."""

    async with session_factory() as session:
        session.add(Job(type=JobType.INSTALL_SSH_KEY, status=JobStatus.SUCCESS))
        session.add(Job(type=JobType.HEALTH_CHECK, status=JobStatus.SUCCESS))
        await session.commit()
    await _alembic("downgrade", BEFORE_SSH_KEY)
    try:
        assert [row[0] for row in await _query(text("SELECT type::text FROM jobs"))] == ["HEALTH_CHECK"]
    finally:
        await _alembic("upgrade", "head")
    assert "INSTALL_SSH_KEY" in {row[0] for row in await _query(JOB_TYPES_SQL)}


# --- журнал аудита: составной индекс (created_at DESC, id DESC) под курсорную пагинацию ---

# Ревизия перед миграцией индекса: откатываем именно её, а не «на одну назад» (поверх могут лежать новые).
BEFORE_AUDIT_INDEX = "0a313252c0db"
AUDIT_INDEX_SQL = text(
    "SELECT indexdef FROM pg_indexes WHERE tablename = 'audit_logs' "
    "AND indexdef ILIKE '%created_at DESC%' AND indexdef ILIKE '%id DESC%'"
)


def _is_cursor_index(definition: object) -> bool:
    normalized = " ".join(str(definition).lower().split())
    return "(created_at desc, id desc)" in normalized


async def test_audit_cursor_index_exists_after_upgrade() -> None:
    assert any(_is_cursor_index(row[0]) for row in await _query(AUDIT_INDEX_SQL))


async def test_audit_cursor_index_downgrade_removes_it_then_upgrade_restores() -> None:
    await _alembic("downgrade", BEFORE_AUDIT_INDEX)
    try:
        assert not any(_is_cursor_index(row[0]) for row in await _query(AUDIT_INDEX_SQL))
    finally:
        await _alembic("upgrade", "head")
    assert any(_is_cursor_index(row[0]) for row in await _query(AUDIT_INDEX_SQL))


# --- смена пароля: admins.{must_change_password, password_changed_at} ---

PASSWORD_COLUMNS_SQL = text(
    """
    SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns
    WHERE table_name = 'admins' AND column_name IN ('must_change_password', 'password_changed_at')
    """
)
BEFORE_PASSWORD_CHANGE = "7c1e4a9d2b30"


async def test_password_change_migration_adds_columns_with_safe_defaults() -> None:
    columns = {row[0]: row for row in await _query(PASSWORD_COLUMNS_SQL)}
    assert set(columns) == {"must_change_password", "password_changed_at"}
    assert columns["must_change_password"][1:3] == ("boolean", "NO") and "false" in str(
        columns["must_change_password"][3]
    )
    assert columns["password_changed_at"][2] == "YES"


async def test_password_change_migration_keeps_existing_admins_unflagged_and_downgrades() -> None:
    """Админы, жившие до миграции, пароли выбирали сами — принудительной смены у них быть не должно."""
    await _alembic("downgrade", BEFORE_PASSWORD_CHANGE)
    try:
        assert await _query(PASSWORD_COLUMNS_SQL) == []
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admins (id, email, password_hash, is_active, role, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), 'old@example.com', 'x', true, 'VIEWER', now(), now())"
                )
            )
    finally:
        await _alembic("upgrade", "head")
    assert await _query(text("SELECT must_change_password, password_changed_at FROM admins")) == [(False, None)]


# --- поиск Amnezia (23.2): protocoltype.{AMNEZIA_APP_AWG, AMNEZIA_APP_AWG2} и jobtype.DISCOVER_VPN ---

# Ревизия перед миграцией поиска (прежняя голова): откатываем именно к ней, а не «на одну назад».
BEFORE_DISCOVERY = "e4f1a7b39c20"
PROTOCOL_TYPES_SQL = text(
    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'protocoltype'"
)
NEW_PROTOCOL_TYPES = {"AMNEZIA_APP_AWG", "AMNEZIA_APP_AWG2"}


async def _labels(sql: object) -> set[object]:
    return {row[0] for row in await _query(sql)}


async def _add_server() -> UUID:
    async with session_factory() as session:
        server = VPNServer(name="migration-srv", host="203.0.113.10")
        session.add(server)
        await session.commit()
        return server.id


async def test_discovery_migration_adds_protocol_and_job_types() -> None:
    assert NEW_PROTOCOL_TYPES | {"AMNEZIA_WG"} <= await _labels(PROTOCOL_TYPES_SQL)
    assert "DISCOVER_VPN" in await _labels(JOB_TYPES_SQL)
    assert "INSTALL_SSH_KEY" in await _labels(JOB_TYPES_SQL)  # прежние значения на месте


async def test_discovery_migration_new_types_are_usable_by_the_orm() -> None:
    """Значения enum в БД совпадают с теми, что пишет код (ORM хранит ИМЕНА членов enum)."""
    from modules.protocols.models.protocol import ProtocolType, VPNProtocol

    server_id = await _add_server()
    async with session_factory() as session:
        session.add(VPNProtocol(server_id=server_id, type=ProtocolType("amnezia_app_awg2"), port=1))
        session.add(Job(type=JobType("discover_vpn"), status=JobStatus.SUCCESS, server_id=server_id))
        await session.commit()
    assert await _query(text("SELECT type::text FROM protocols")) == [("AMNEZIA_APP_AWG2",)]
    assert await _query(text("SELECT type::text FROM jobs")) == [("DISCOVER_VPN",)]


async def test_discovery_migration_downgrade_removes_new_values_then_upgrade_restores() -> None:
    await _alembic("downgrade", BEFORE_DISCOVERY)
    try:
        assert await _labels(PROTOCOL_TYPES_SQL) == {"AMNEZIA_WG"}
        assert "DISCOVER_VPN" not in await _labels(JOB_TYPES_SQL)
        assert "INSTALL_SSH_KEY" in await _labels(JOB_TYPES_SQL)
    finally:
        await _alembic("upgrade", "head")
    assert NEW_PROTOCOL_TYPES <= await _labels(PROTOCOL_TYPES_SQL)
    assert "DISCOVER_VPN" in await _labels(JOB_TYPES_SQL)


async def test_discovery_migration_downgrade_deletes_rows_of_new_types_and_keeps_others() -> None:
    """Значение enum нельзя убрать, пока есть строки с ним: откат сначала удаляет протоколы и задачи новых типов
    (что панель «подключила» из найденного — теряется), остальное цело."""
    server_id = await _add_server()
    async with engine.begin() as conn:
        for protocol_type in ("AMNEZIA_WG", "AMNEZIA_APP_AWG", "AMNEZIA_APP_AWG2"):
            await conn.execute(
                text("INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :sid, :type)"),
                {"sid": server_id, "type": protocol_type},
            )
        for job_type in ("DISCOVER_VPN", "HEALTH_CHECK", "INSTALL_SSH_KEY"):
            await conn.execute(
                text("INSERT INTO jobs (id, type, status) VALUES (gen_random_uuid(), :type, 'SUCCESS')"),
                {"type": job_type},
            )
    await _alembic("downgrade", BEFORE_DISCOVERY)
    try:
        assert await _query(text("SELECT type::text FROM protocols")) == [("AMNEZIA_WG",)]
        assert sorted(row[0] for row in await _query(text("SELECT type::text FROM jobs"))) == [
            "HEALTH_CHECK",
            "INSTALL_SSH_KEY",
        ]
        assert await _labels(PROTOCOL_TYPES_SQL) == {"AMNEZIA_WG"}
    finally:
        await _alembic("upgrade", "head")
    assert NEW_PROTOCOL_TYPES <= await _labels(PROTOCOL_TYPES_SQL)


async def test_discovery_migration_downgrade_keeps_protocols_table_defaults() -> None:
    """Пересоздание enum не должно потерять уникальность (server_id, type) и значение по умолчанию."""
    await _alembic("downgrade", BEFORE_DISCOVERY)
    try:
        server_id = await _add_server()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :sid, 'AMNEZIA_WG')"),
                {"sid": server_id},
            )
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :sid, 'AMNEZIA_WG')"),
                    {"sid": server_id},
                )
        except Exception as exc:  # noqa: BLE001 — нужен именно отказ БД по уникальному ограничению
            assert "unique" in str(exc).lower()
        else:
            raise AssertionError("UniqueConstraint(server_id, type) was lost by the downgrade")
    finally:
        await _alembic("upgrade", "head")
