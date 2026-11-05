"""Protocol image replacement and read-only availability check jobs.

Revision ID: a9d1e6c42b70
Revises: e7a4c1d92b58
"""

from alembic import op

revision = "a9d1e6c42b70"
down_revision = "e7a4c1d92b58"
branch_labels = None
depends_on = None

_BEFORE = (
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
    "GATEWAY_ADD_EXIT",
    "GATEWAY_REMOVE_EXIT",
    "GEOIP_UPDATE",
    "GATEWAY_APPLY_RU",
    "GATEWAY_APPLY",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'UPDATE_PROTOCOL'")
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'CHECK_PROTOCOL_UPDATES'")


def downgrade() -> None:
    # Older application versions cannot deserialize these jobs. Like prior enum migrations,
    # discard only their history; unrelated jobs, protocol/client data and audit survive.
    op.execute("DELETE FROM jobs WHERE type::text IN ('UPDATE_PROTOCOL', 'CHECK_PROTOCOL_UPDATES')")
    op.execute("ALTER TABLE jobs ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE jobtype RENAME TO jobtype_old")
    values = ", ".join(f"'{value}'" for value in _BEFORE)
    op.execute(f"CREATE TYPE jobtype AS ENUM ({values})")
    op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE jobtype USING type::text::jobtype")
    op.execute("ALTER TABLE jobs ALTER COLUMN type SET DEFAULT 'HEALTH_CHECK'")
    op.execute("DROP TYPE jobtype_old")
