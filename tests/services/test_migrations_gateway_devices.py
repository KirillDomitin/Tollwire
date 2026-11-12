"""Device migration preserves legacy secrets and refuses destructive compatibility downgrades."""

import pytest
from sqlalchemy import text

from core.database.postgres import engine
from modules.jobs.models.job import Job, JobStatus, JobType
from tests.conftest import run_alembic
from tests.fixtures import World
from tests.services.test_gateway_devices import gateway, target

BEFORE_DEVICES = "a9d1e6c42b70"
DEVICES = "g8c2e4a61d90"


async def migrate(action: str, revision: str) -> None:
    await engine.dispose()
    try:
        run_alembic(action, revision)
    finally:
        await engine.dispose()


async def test_device_downgrade_preserves_completed_jobs_with_legacy_types(world: World) -> None:
    created = Job(type=JobType.CREATE_DEVICE, status=JobStatus.SUCCESS)
    revoked = Job(type=JobType.REVOKE_DEVICE, status=JobStatus.FAILED)
    world.session.add_all([created, revoked])
    await world.session.commit()
    expected = {created.id: ("CREATE_USER", "SUCCESS"), revoked.id: ("REVOKE_ACCESS", "FAILED")}
    await world.session.close()
    await migrate("downgrade", BEFORE_DEVICES)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text("SELECT id, type::text, status::text FROM jobs"))).all()
        assert {row[0]: (row[1], row[2]) for row in rows} == expected
    finally:
        await migrate("upgrade", "head")


@pytest.mark.parametrize("kind", [JobType.CREATE_USER, JobType.REVOKE_ACCESS])
async def test_device_downgrade_refuses_unfinished_targeted_legacy_jobs(world: World, kind: JobType) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    _, _, grant, credential = await world.issued(protocol)
    pending = Job(type=kind, status=JobStatus.PENDING, server_id=protocol.server_id,
                  access_grant_id=grant.id, params={"credential_id": str(credential.id)})
    world.session.add(pending)
    await world.session.commit()
    pending_id, credential_id = pending.id, credential.id
    await world.session.close()
    try:
        with pytest.raises(RuntimeError, match="unfinished device jobs"):
            await migrate("downgrade", BEFORE_DEVICES)
        async with engine.connect() as connection:
            rows = (await connection.execute(text(
                "SELECT id FROM vpn_credentials WHERE id = :id"
            ), {"id": credential_id})).scalars().all()
            job = (await connection.execute(text(
                "SELECT type::text, status::text FROM jobs WHERE id = :id"
            ), {"id": pending_id})).one()
        assert rows == [credential_id] and tuple(job) == (kind.name, "PENDING")
    finally:
        await migrate("upgrade", "head")


async def test_device_migration_roundtrip_preserves_legacy_keys_configs_and_gateway_owner(world: World) -> None:
    protocol, _ = await gateway(world)
    _, _, grant = await world.access(protocol)
    credential_id = target(await world.credentials().schedule_issue(grant.id))
    credential = await world.credentials().provision(grant.id, credential_id=credential_id)
    identity = (credential.client_public_key, credential.client_address, credential.encrypted_config)
    await world.session.execute(text("UPDATE jobs SET status = 'SUCCESS'"))
    await world.session.commit()
    await world.session.close()
    await migrate("downgrade", BEFORE_DEVICES)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(
                text("SELECT client_public_key, client_address, encrypted_config FROM vpn_credentials WHERE id = :id"),
                {"id": credential_id},
            )).one()
            assert tuple(row) == identity
            columns = (await connection.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'vpn_credentials'"
            ))).scalars().all()
            assert "state" not in columns and "is_gateway" not in columns
    finally:
        await migrate("upgrade", "head")
    async with engine.connect() as connection:
        row = (await connection.execute(text(
            "SELECT client_public_key, client_address, encrypted_config, state, is_gateway "
            "FROM vpn_credentials WHERE id = :id"
        ), {"id": credential_id})).one()
    assert tuple(row[:3]) == identity
    assert tuple(row[3:]) == ("ACTIVE", True)


@pytest.mark.parametrize("incompatible", ["reservation", "multiple_live", "unfinished_job"])
async def test_device_downgrade_refuses_without_deleting_incompatible_data(world: World, incompatible: str) -> None:
    protocol, _ = await gateway(world)
    _, _, grant = await world.access(protocol)
    first = target(await world.credentials().schedule_issue(grant.id))
    if incompatible != "reservation":
        await world.credentials().provision(grant.id, credential_id=first)
    if incompatible == "multiple_live":
        second = target(await world.credentials().schedule_issue(grant.id))
        await world.credentials().provision(grant.id, credential_id=second)
    if incompatible != "unfinished_job":
        await world.session.execute(text("UPDATE jobs SET status = 'SUCCESS'"))
    await world.session.commit()
    before = (await world.session.execute(text("SELECT id FROM vpn_credentials ORDER BY id"))).scalars().all()
    await world.session.close()
    try:
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            await migrate("downgrade", BEFORE_DEVICES)
        async with engine.connect() as connection:
            after = (await connection.execute(text("SELECT id FROM vpn_credentials ORDER BY id"))).scalars().all()
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert after == before
        assert version == DEVICES
    finally:
        await migrate("upgrade", "head")
