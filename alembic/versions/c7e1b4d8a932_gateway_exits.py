"""gateway exits

Revision ID: c7e1b4d8a932
Revises: a8c4e1f7b925
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c7e1b4d8a932"
down_revision: Union[str, Sequence[str], None] = "a8c4e1f7b925"
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
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'GATEWAY_ADD_EXIT'")
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'GATEWAY_REMOVE_EXIT'")

    op.create_table(
        "gateway_exits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("gateway_protocol_id", sa.Uuid(), nullable=False),
        sa.Column("exit_protocol_id", sa.Uuid(), nullable=False),
        sa.Column("slot", sa.SmallInteger(), nullable=False),
        sa.Column("weight", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("max_clients", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("health", sa.String(length=16), nullable=True),
        sa.Column("last_handshake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_public_key", sa.String(length=64), nullable=True),
        sa.Column("client_address", sa.String(length=64), nullable=True),
        sa.Column("encrypted_config", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("slot BETWEEN 1 AND 200", name="ck_gateway_exits_slot"),
        sa.CheckConstraint("weight BETWEEN 1 AND 100", name="ck_gateway_exits_weight"),
        sa.CheckConstraint("max_clients IS NULL OR max_clients > 0", name="ck_gateway_exits_max_clients"),
        sa.ForeignKeyConstraint(
            ["gateway_protocol_id"], ["protocols.id"], name="gateway_exits_gateway_protocol_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["exit_protocol_id"], ["protocols.id"], name="gateway_exits_exit_protocol_id_fkey", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gateway_protocol_id", "slot", name="uq_gateway_exits_slot"),
        sa.UniqueConstraint("gateway_protocol_id", "exit_protocol_id", name="uq_gateway_exits_exit"),
    )
    op.create_index("ix_gateway_exits_exit_protocol_id", "gateway_exits", ["exit_protocol_id"])


def downgrade() -> None:
    # Выходы исчезают вместе с таблицей (туннели на шлюзах и внутренние клиенты на выходах панель при откате не
    # трогает: их подберёт sync более ранней версии как чужих). Значение из enum Postgres не убрать, пока есть строки
    # с ним: задачи этих типов удаляются, затем тип пересоздаётся без новых значений (как f6b2d8a41c73).
    op.drop_index("ix_gateway_exits_exit_protocol_id", table_name="gateway_exits")
    op.drop_table("gateway_exits")

    op.execute("DELETE FROM jobs WHERE type IN ('GATEWAY_ADD_EXIT', 'GATEWAY_REMOVE_EXIT')")
    op.execute("ALTER TABLE jobs ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE jobtype RENAME TO jobtype_old")
    values = ", ".join(f"'{value}'" for value in _JOB_TYPES_BEFORE)
    op.execute(f"CREATE TYPE jobtype AS ENUM ({values})")
    op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE jobtype USING type::text::jobtype")
    op.execute("ALTER TABLE jobs ALTER COLUMN type SET DEFAULT 'HEALTH_CHECK'")
    op.execute("DROP TYPE jobtype_old")
