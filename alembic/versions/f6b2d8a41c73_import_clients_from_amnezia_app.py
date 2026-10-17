"""import clients from amnezia app

Revision ID: f6b2d8a41c73
Revises: d3a7c9e15b42
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f6b2d8a41c73"
down_revision: Union[str, Sequence[str], None] = "d3a7c9e15b42"
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
)
_CHECK = "ck_vpn_credentials_issued_has_config"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'IMPORT_CLIENTS'")

    # Подхваченные из AmneziaVPN клиенты: email у них не было, а конфига (приватного ключа) у панели нет.
    op.alter_column("vpn_users", "email", existing_type=sa.String(length=100), nullable=True)
    op.alter_column("vpn_credentials", "encrypted_config", existing_type=sa.Text(), nullable=True)
    op.add_column(
        "vpn_credentials",
        sa.Column("source", sa.String(length=16), server_default=sa.text("'ISSUED'"), nullable=False),
    )
    op.add_column("vpn_credentials", sa.Column("encrypted_psk", sa.Text(), nullable=True))
    op.add_column("vpn_credentials", sa.Column("external_name", sa.String(length=100), nullable=True))
    op.create_check_constraint(_CHECK, "vpn_credentials", "source <> 'ISSUED' OR encrypted_config IS NOT NULL")
    op.add_column("jobs", sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    # Импортированных клиентов старая схема не вместит (нет конфига): их credential'ы уходят. Доступ уходит с
    # ними, только если на нём не осталось обычных credential'ов (после отзыва импортированного по тому же гранту
    # могли выпустить обычный: его трогать нельзя). Пользователи и подписки остаются. Peer'ы на сервере не
    # трогаются: это чужие клиенты приложения.
    op.execute(
        "DELETE FROM access_grants WHERE id IN "
        "(SELECT access_grant_id FROM vpn_credentials WHERE source = 'IMPORTED') "
        "AND NOT EXISTS (SELECT 1 FROM vpn_credentials c "
        "WHERE c.access_grant_id = access_grants.id AND c.source <> 'IMPORTED')"
    )
    op.execute("DELETE FROM vpn_credentials WHERE source = 'IMPORTED'")
    op.execute("DELETE FROM jobs WHERE type = 'IMPORT_CLIENTS'")
    op.execute("UPDATE vpn_users SET email = id::text || '@imported.invalid' WHERE email IS NULL")

    op.drop_column("jobs", "params")
    op.drop_constraint(_CHECK, "vpn_credentials", type_="check")
    op.drop_column("vpn_credentials", "external_name")
    op.drop_column("vpn_credentials", "encrypted_psk")
    op.drop_column("vpn_credentials", "source")
    op.alter_column("vpn_credentials", "encrypted_config", existing_type=sa.Text(), nullable=False)
    op.alter_column("vpn_users", "email", existing_type=sa.String(length=100), nullable=False)

    op.execute("ALTER TABLE jobs ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE jobtype RENAME TO jobtype_old")
    values = ", ".join(f"'{value}'" for value in _JOB_TYPES_BEFORE)
    op.execute(f"CREATE TYPE jobtype AS ENUM ({values})")
    op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE jobtype USING type::text::jobtype")
    op.execute("ALTER TABLE jobs ALTER COLUMN type SET DEFAULT 'HEALTH_CHECK'")
    op.execute("DROP TYPE jobtype_old")
