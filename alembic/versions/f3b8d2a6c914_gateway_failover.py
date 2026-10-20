"""gateway failover: exit health counters, gateway_apply job

Revision ID: f3b8d2a6c914
Revises: e5a1c9f3b207
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "f3b8d2a6c914"
down_revision: Union[str, Sequence[str], None] = "e5a1c9f3b207"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JOB_TYPES_BEFORE = (
    "CREATE_USER",
    "DELETE_USER",
    "SYNC_SERVER",
    "HEALTH_CHECK",
    "REVOKE_ACCESS",
    "INSTALL_SERVER",
    "REMOVE_PROTOCOL",
    "UNINSTALL_SERVER",
    "INSTALL_PROTOCOL",
    "INSTALL_SSH_KEY",
    "DISCOVER_VPN",
    "IMPORT_CLIENTS",
    "GATEWAY_ADD_EXIT",
    "GATEWAY_REMOVE_EXIT",
    "GEOIP_UPDATE",
    "GATEWAY_APPLY_RU",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'GATEWAY_APPLY'")
    op.add_column(
        "gateway_exits", sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column("gateway_exits", sa.Column("healthy_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "gateway_exits", sa.Column("failure_notified", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("gateway_exits", "failure_notified")
    op.drop_column("gateway_exits", "healthy_since")
    op.drop_column("gateway_exits", "consecutive_failures")

    # Значение из enum Postgres не убрать, пока есть строки с ним: задачи типа удаляются, тип пересоздаётся.
    op.execute("DELETE FROM jobs WHERE type = 'GATEWAY_APPLY'")
    op.execute("ALTER TABLE jobs ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE jobtype RENAME TO jobtype_old")
    values = ", ".join(f"'{value}'" for value in _JOB_TYPES_BEFORE)
    op.execute(f"CREATE TYPE jobtype AS ENUM ({values})")
    op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE jobtype USING type::text::jobtype")
    op.execute("ALTER TABLE jobs ALTER COLUMN type SET DEFAULT 'HEALTH_CHECK'")
    op.execute("DROP TYPE jobtype_old")
