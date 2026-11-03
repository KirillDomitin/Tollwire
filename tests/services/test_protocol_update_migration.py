"""Update job type is usable in PostgreSQL and survives migration roundtrip."""

from uuid import uuid4

from sqlalchemy import text

from core.database.postgres import engine, session_factory
from modules.jobs.models.job import Job, JobType
from tests.conftest import run_alembic


async def test_update_job_enum_is_installed_and_roundtrips() -> None:
    async with engine.connect() as conn:
        values = (
            (
                await conn.execute(
                    text(
                        "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname='jobtype'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert "UPDATE_PROTOCOL" in values, "migration must extend PostgreSQL jobtype"
    assert "CHECK_PROTOCOL_UPDATES" in values, "migration must support read-only availability checks"
    kind = getattr(JobType, "UPDATE_PROTOCOL", None)
    assert kind is not None and kind.value == "update_protocol"
    check_kind = getattr(JobType, "CHECK_PROTOCOL_UPDATES", None)
    assert check_kind is not None and check_kind.value == "check_protocol_updates"
    async with session_factory() as session:
        job = Job(id=uuid4(), type=kind, params={"protocol_type": "amnezia_wg"})
        session.add(job)
        await session.commit()
        await session.refresh(job)
        assert job.type is kind


async def test_update_job_enum_survives_full_downgrade_upgrade() -> None:
    # No unreleased revision ID is hardcoded; this verifies the real migration chain.
    # Once its revision exists, add targeted downgrade with an existing UPDATE_PROTOCOL job;
    # a base roundtrip alone does not prove that data-preserving downgrade policy.
    await engine.dispose()
    try:
        run_alembic("downgrade", "base")
        run_alembic("upgrade", "head")
    finally:
        await engine.dispose()
    async with engine.connect() as conn:
        values = (
            (
                await conn.execute(
                    text(
                        "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname='jobtype'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert "UPDATE_PROTOCOL" in values
    assert "CHECK_PROTOCOL_UPDATES" in values
