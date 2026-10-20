"""gateway assignments

Revision ID: d2f8a6c3b514
Revises: c7e1b4d8a932
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d2f8a6c3b514"
down_revision: Union[str, Sequence[str], None] = "c7e1b4d8a932"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gateway_assignments",
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("exit_id", sa.Uuid(), nullable=False),
        sa.Column("pinned_exit_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reason", sa.String(length=16), server_default=sa.text("'ISSUE'"), nullable=False),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["vpn_credentials.id"], name="gateway_assignments_credential_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["exit_id"], ["gateway_exits.id"], name="gateway_assignments_exit_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pinned_exit_id"],
            ["gateway_exits.id"],
            name="gateway_assignments_pinned_exit_id_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("credential_id"),
    )
    op.create_index("ix_gateway_assignments_exit_id", "gateway_assignments", ["exit_id"])
    # Итог переноса клиентов при снятии выхода (счётчики аудита переживают повтор задачи).
    op.add_column(
        "gateway_exits", sa.Column("drained_moved", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column(
        "gateway_exits", sa.Column("drained_unassigned", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )


def downgrade() -> None:
    # Назначения - производные данные (карта на шлюзе собирается из БД): таблица просто исчезает; файл assign.rules и
    # цепочка GW_ASSIGN на шлюзах остаются до следующей установки шлюза более ранней версии.
    op.drop_column("gateway_exits", "drained_unassigned")
    op.drop_column("gateway_exits", "drained_moved")
    op.drop_index("ix_gateway_assignments_exit_id", table_name="gateway_assignments")
    op.drop_table("gateway_assignments")
