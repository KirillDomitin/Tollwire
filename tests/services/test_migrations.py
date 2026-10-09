"""Миграции целиком: откат до нуля и обратно до head.

Ловит ошибки, на которые проект уже наступал: забытый DROP TYPE для enum в downgrade (11.1, 13.2),
безымянный внешний ключ, который downgrade не может удалить (12.3, 14.1).
"""

from sqlalchemy import text

from core.database.postgres import engine, session_factory
from modules.jobs.models.job import Job, JobStatus, JobType
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
    await _alembic("downgrade", "-1")
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
    await _alembic("downgrade", "-1")
    try:
        assert [row[0] for row in await _query(text("SELECT type::text FROM jobs"))] == ["HEALTH_CHECK"]
    finally:
        await _alembic("upgrade", "head")
    assert "INSTALL_SSH_KEY" in {row[0] for row in await _query(JOB_TYPES_SQL)}
