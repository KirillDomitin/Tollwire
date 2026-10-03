"""admin role and last login

Revision ID: ad2d9e3a0025
Revises: 4ddd52ceed12
Create Date: 2026-10-03 18:13:00.663653

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ad2d9e3a0025"
down_revision: Union[str, Sequence[str], None] = "4ddd52ceed12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    role = sa.Enum("SUPERADMIN", "OPERATOR", "VIEWER", name="adminrole")
    role.create(op.get_bind(), checkfirst=True)
    # Существующие админы до RBAC могли всё — остаются superadmin (иначе панель останется без управляющих).
    # Затем умолчание БД снимается: роль новых админов задаёт приложение (по умолчанию viewer).
    op.add_column("admins", sa.Column("role", role, nullable=False, server_default="SUPERADMIN"))
    op.alter_column("admins", "role", server_default=None)
    op.add_column("admins", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("admins", "last_login_at")
    op.drop_column("admins", "role")
    sa.Enum(name="adminrole").drop(op.get_bind(), checkfirst=True)
