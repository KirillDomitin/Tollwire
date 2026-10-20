"""geoip lists and overrides, russian-direct jobs

Revision ID: e5a1c9f3b207
Revises: d2f8a6c3b514
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e5a1c9f3b207"
down_revision: Union[str, Sequence[str], None] = "d2f8a6c3b514"
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
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'GEOIP_UPDATE'")
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'GATEWAY_APPLY_RU'")

    op.create_table(
        "geoip_lists",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("prefixes_count", sa.Integer(), nullable=False),
        sa.Column("addresses_count", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_geoip_lists_one_active",
        "geoip_lists",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "geoip_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("network", sa.String(length=18), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("network", name="uq_geoip_overrides_network"),
    )


def downgrade() -> None:
    op.drop_table("geoip_overrides")
    op.drop_index("uq_geoip_lists_one_active", table_name="geoip_lists", postgresql_where=sa.text("status = 'ACTIVE'"))
    op.drop_table("geoip_lists")

    # Значение из enum Postgres не убрать, пока есть строки с ним: задачи этих типов удаляются, тип пересоздаётся.
    op.execute("DELETE FROM jobs WHERE type IN ('GEOIP_UPDATE', 'GATEWAY_APPLY_RU')")
    op.execute("ALTER TABLE jobs ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE jobtype RENAME TO jobtype_old")
    values = ", ".join(f"'{value}'" for value in _JOB_TYPES_BEFORE)
    op.execute(f"CREATE TYPE jobtype AS ENUM ({values})")
    op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE jobtype USING type::text::jobtype")
    op.execute("ALTER TABLE jobs ALTER COLUMN type SET DEFAULT 'HEALTH_CHECK'")
    op.execute("DROP TYPE jobtype_old")
