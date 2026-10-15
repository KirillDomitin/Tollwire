"""admin must_change_password and password_changed_at

Revision ID: e4f1a7b39c20
Revises: 7c1e4a9d2b30
Create Date: 2026-10-15 22:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4f1a7b39c20"
down_revision: Union[str, Sequence[str], None] = "7c1e4a9d2b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Существующие админы сами выбрали свои пароли — менять их принудительно не нужно.
    op.add_column("admins", sa.Column("must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("admins", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("admins", "password_changed_at")
    op.drop_column("admins", "must_change_password")
