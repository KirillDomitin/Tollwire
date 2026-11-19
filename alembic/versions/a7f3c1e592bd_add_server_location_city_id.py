"""Add the optional server city identifier."""

import sqlalchemy as sa

from alembic import op

revision = "a7f3c1e592bd"
down_revision = "e9b3d7c10f42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vpn_servers", sa.Column("location_city_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("vpn_servers", "location_city_id")
