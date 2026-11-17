"""Version assignments, including older workers and FK actions."""

import sqlalchemy as sa

from alembic import op

revision = "e9b3d7c10f42"
down_revision = "g8c2e4a61d90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "gateway_assignments",
        sa.Column("revision", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
    )
    op.execute("""
        CREATE FUNCTION gateway_assignment_revision() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.exit_id IS DISTINCT FROM OLD.exit_id
               OR NEW.pinned_exit_id IS DISTINCT FROM OLD.pinned_exit_id THEN
                NEW.revision := gen_random_uuid();
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER gateway_assignment_revision
        BEFORE UPDATE ON gateway_assignments
        FOR EACH ROW EXECUTE FUNCTION gateway_assignment_revision()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER gateway_assignment_revision ON gateway_assignments")
    op.execute("DROP FUNCTION gateway_assignment_revision()")
    op.drop_column("gateway_assignments", "revision")
