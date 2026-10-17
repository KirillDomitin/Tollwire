"""discover amnezia app

Revision ID: d3a7c9e15b42
Revises: e4f1a7b39c20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d3a7c9e15b42"
down_revision: Union[str, Sequence[str], None] = "e4f1a7b39c20"
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
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE protocoltype ADD VALUE IF NOT EXISTS 'AMNEZIA_APP_AWG'")
        op.execute("ALTER TYPE protocoltype ADD VALUE IF NOT EXISTS 'AMNEZIA_APP_AWG2'")
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'DISCOVER_VPN'")


def downgrade() -> None:
    op.execute("DELETE FROM protocols WHERE type IN ('AMNEZIA_APP_AWG', 'AMNEZIA_APP_AWG2')")
    op.execute("ALTER TYPE protocoltype RENAME TO protocoltype_old")
    op.execute("CREATE TYPE protocoltype AS ENUM ('AMNEZIA_WG')")
    op.execute("ALTER TABLE protocols ALTER COLUMN type TYPE protocoltype USING type::text::protocoltype")
    op.execute("DROP TYPE protocoltype_old")

    op.execute("DELETE FROM jobs WHERE type = 'DISCOVER_VPN'")
    op.execute("ALTER TABLE jobs ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE jobtype RENAME TO jobtype_old")
    values = ", ".join(f"'{value}'" for value in _JOB_TYPES_BEFORE)
    op.execute(f"CREATE TYPE jobtype AS ENUM ({values})")
    op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE jobtype USING type::text::jobtype")
    op.execute("ALTER TABLE jobs ALTER COLUMN type SET DEFAULT 'HEALTH_CHECK'")
    op.execute("DROP TYPE jobtype_old")
