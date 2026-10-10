"""audit logs cursor index

Revision ID: 7c1e4a9d2b30
Revises: 0a313252c0db
Create Date: 2026-10-10 00:40:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1e4a9d2b30"
down_revision: Union[str, Sequence[str], None] = "0a313252c0db"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ix_audit_logs_created_at_id"
# Одиночный индекс по created_at — префикс нового, держать оба незачем: запись аудита идёт в транзакции действия.
_OLD_INDEX = "ix_audit_logs_created_at"


def upgrade() -> None:
    # CONCURRENTLY: запись аудита идёт в одной транзакции с действием, блокировать её на большой таблице нельзя.
    with op.get_context().autocommit_block():
        # Оборванный CONCURRENTLY оставляет INVALID-индекс, который IF NOT EXISTS принял бы за готовый.
        op.drop_index(_INDEX, table_name="audit_logs", postgresql_concurrently=True, if_exists=True)
        op.create_index(
            _INDEX,
            "audit_logs",
            [sa.text("created_at DESC"), sa.text("id DESC")],
            postgresql_concurrently=True,
        )
        op.drop_index(_OLD_INDEX, table_name="audit_logs", postgresql_concurrently=True, if_exists=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(_OLD_INDEX, "audit_logs", ["created_at"], postgresql_concurrently=True, if_not_exists=True)
        op.drop_index(_INDEX, table_name="audit_logs", postgresql_concurrently=True, if_exists=True)
