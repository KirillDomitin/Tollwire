"""Revision migration backfills existing assignments and preserves routing identity."""

from sqlalchemy import text

from core.database.postgres import engine
from tests.fixtures import World
from tests.integration.test_bot_device_exit_switch import SwitchScene, switch  # noqa: F401
from tests.services.test_migrations_gateway_devices import migrate


async def test_assignment_revision_upgrade_downgrade_preserves_route(switch: SwitchScene, world: World) -> None:  # noqa: F811
    async with engine.connect() as connection:
        before = (
            await connection.execute(text("SELECT credential_id, exit_id, pinned_exit_id FROM gateway_assignments"))
        ).all()
    assert before
    await world.session.close()
    await migrate("downgrade", "g8c2e4a61d90")
    try:
        async with engine.connect() as connection:
            columns = (
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_name='gateway_assignments'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "revision" not in columns
            assert (
                await connection.execute(text("SELECT credential_id, exit_id, pinned_exit_id FROM gateway_assignments"))
            ).all() == before
        await migrate("upgrade", "head")
        async with engine.begin() as connection:
            revision = (
                await connection.execute(
                    text("SELECT revision FROM gateway_assignments WHERE credential_id=:id"), {"id": before[0][0]}
                )
            ).scalar_one()
            assert revision is not None
            changed = (
                await connection.execute(
                    text(
                        "UPDATE gateway_assignments SET pinned_exit_id=exit_id "
                        "WHERE credential_id=:id RETURNING revision"
                    ),
                    {"id": before[0][0]},
                )
            ).scalar_one()
            assert changed != revision
            unchanged = (
                await connection.execute(
                    text(
                        "UPDATE gateway_assignments SET pinned_exit_id=exit_id "
                        "WHERE credential_id=:id RETURNING revision"
                    ),
                    {"id": before[0][0]},
                )
            ).scalar_one()
            assert unchanged == changed
            unpinned = (
                await connection.execute(
                    text(
                        "UPDATE gateway_assignments SET pinned_exit_id=NULL WHERE credential_id=:id RETURNING revision"
                    ),
                    {"id": before[0][0]},
                )
            ).scalar_one()
            assert unpinned not in (changed, revision)
    finally:
        await migrate("upgrade", "head")
