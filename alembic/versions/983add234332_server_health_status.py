"""server health status

Revision ID: 983add234332
Revises: b41808964fd0
Create Date: 2026-10-03 00:42:28.107551

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "983add234332"
down_revision: Union[str, Sequence[str], None] = "b41808964fd0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum-колонка добавляется в существующую таблицу (ALTER), поэтому тип Postgres создаём и удаляем явно.
health_status = sa.Enum("OK", "DEGRADED", "DOWN", name="healthstatus")


def upgrade() -> None:
    """Upgrade schema."""
    health_status.create(op.get_bind(), checkfirst=True)
    op.add_column("vpn_servers", sa.Column("health_status", health_status, nullable=True))
    op.add_column("vpn_servers", sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("vpn_servers", "last_health_at")
    op.drop_column("vpn_servers", "health_status")
    health_status.drop(op.get_bind(), checkfirst=True)
