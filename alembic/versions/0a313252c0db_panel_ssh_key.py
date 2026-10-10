"""panel ssh key

Revision ID: 0a313252c0db
Revises: b587ddfe2d19
Create Date: 2026-10-09 22:53:57.343026

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0a313252c0db"
down_revision: Union[str, Sequence[str], None] = "b587ddfe2d19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# jobtype без INSTALL_SSH_KEY — каким он был до этой ревизии (порядок значений важен для сортировки).
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
)


def upgrade() -> None:
    """Upgrade schema."""
    # ALTER TYPE … ADD VALUE нельзя выполнить в одной транзакции с использованием нового значения.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'INSTALL_SSH_KEY'")
    op.add_column(
        "server_credentials", sa.Column("managed_key", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("server_credentials", sa.Column("public_key", sa.Text(), nullable=True))
    op.add_column("server_credentials", sa.Column("key_fingerprint", sa.String(length=100), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("server_credentials", "key_fingerprint")
    op.drop_column("server_credentials", "public_key")
    op.drop_column("server_credentials", "managed_key")
    # Значение из enum в PostgreSQL не удалить: тип пересоздаётся без него. Задачи этого типа (история)
    # удаляются — иначе приведение к старому типу упадёт; аудит их результатов остаётся.
    op.execute("DELETE FROM jobs WHERE type = 'INSTALL_SSH_KEY'")
    op.execute("ALTER TABLE jobs ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE jobtype RENAME TO jobtype_old")
    values = ", ".join(f"'{value}'" for value in _JOB_TYPES_BEFORE)
    op.execute(f"CREATE TYPE jobtype AS ENUM ({values})")
    op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE jobtype USING type::text::jobtype")
    op.execute("ALTER TABLE jobs ALTER COLUMN type SET DEFAULT 'HEALTH_CHECK'")
    op.execute("DROP TYPE jobtype_old")
