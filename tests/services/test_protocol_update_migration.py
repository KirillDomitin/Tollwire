"""Update job type is usable in PostgreSQL and survives migration roundtrip."""

from uuid import uuid4

from sqlalchemy import text

from core.database.postgres import engine, session_factory
from modules.jobs.models.job import Job, JobStatus, JobType
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


async def test_targeted_downgrade_removes_only_new_job_types_and_preserves_old_job_data() -> None:
    update_kind = getattr(JobType, "UPDATE_PROTOCOL", None)
    check_kind = getattr(JobType, "CHECK_PROTOCOL_UPDATES", None)
    assert update_kind is not None and check_kind is not None
    old_id, update_id, check_id = uuid4(), uuid4(), uuid4()
    async with session_factory() as session:
        session.add_all(
            [
                Job(
                    id=old_id,
                    type=JobType.HEALTH_CHECK,
                    status=JobStatus.SUCCESS,
                    params={"preserve": "input"},
                    result={"preserve": "output"},
                    progress=100,
                ),
                Job(id=update_id, type=update_kind, status=JobStatus.SUCCESS, params={"protocol_type": "amnezia_wg"}),
                Job(id=check_id, type=check_kind, status=JobStatus.SUCCESS),
            ]
        )
        await session.commit()
    await engine.dispose()
    try:
        run_alembic("downgrade", "e7a4c1d92b58")
        await engine.dispose()
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT id, type::text, status::text, params, result, progress FROM jobs"))
            ).all()
            labels = (
                (
                    await conn.execute(
                        text(
                            "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid "
                            "WHERE t.typname='jobtype'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [tuple(row) for row in rows] == [
            (old_id, "HEALTH_CHECK", "SUCCESS", {"preserve": "input"}, {"preserve": "output"}, 100)
        ]
        assert "UPDATE_PROTOCOL" not in labels and "CHECK_PROTOCOL_UPDATES" not in labels
    finally:
        await engine.dispose()
        run_alembic("upgrade", "a9d1e6c42b70")
        await engine.dispose()
    async with session_factory() as session:
        old = await session.get(Job, old_id)
        assert old is not None and old.result == {"preserve": "output"}
        assert await session.get(Job, update_id) is None and await session.get(Job, check_id) is None
        session.add_all([Job(type=update_kind), Job(type=check_kind)])
        await session.commit()  # Both values are usable again after upgrade.
