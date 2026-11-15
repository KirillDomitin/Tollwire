"""Reserve independent gateway credentials before SSH.

Revision ID: g8c2e4a61d90
Revises: a9d1e6c42b70
"""

import sqlalchemy as sa

from alembic import op

revision: str = "g8c2e4a61d90"
down_revision: str = "a9d1e6c42b70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'CREATE_DEVICE'")
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'REVOKE_DEVICE'")
    op.add_column("vpn_credentials", sa.Column("state", sa.String(16), nullable=False, server_default="ACTIVE"))
    op.add_column("vpn_credentials", sa.Column("is_gateway", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("vpn_credentials", sa.Column("device_name", sa.String(100), nullable=True))
    op.add_column("vpn_credentials", sa.Column("peer_name", sa.String(64), nullable=True))
    op.add_column("vpn_credentials", sa.Column("request_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_vpn_credentials_request_id", "vpn_credentials", ["request_id"])
    op.execute("UPDATE vpn_credentials SET state = 'REVOKED' WHERE revoked_at IS NOT NULL")
    op.execute("""UPDATE vpn_credentials c SET is_gateway = true FROM access_grants g
                  JOIN protocols p ON p.id = g.protocol_id
                  WHERE c.access_grant_id = g.id AND p.type = 'AMNEZIA_GATEWAY'""")
    op.drop_index("uq_vpn_credentials_live_per_grant", table_name="vpn_credentials")
    op.create_index(
        "uq_vpn_credentials_live_per_grant",
        "vpn_credentials",
        ["access_grant_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND NOT is_gateway"),
    )
    op.alter_column("vpn_credentials", "client_address", existing_type=sa.String(64), nullable=True)
    op.alter_column("vpn_credentials", "client_public_key", existing_type=sa.String(64), nullable=True)
    op.drop_constraint("ck_vpn_credentials_issued_has_config", "vpn_credentials", type_="check")
    op.create_check_constraint(
        "ck_vpn_credentials_issued_has_config",
        "vpn_credentials",
        "state <> 'ACTIVE' OR source <> 'ISSUED' OR encrypted_config IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_vpn_credentials_active_identity",
        "vpn_credentials",
        "state <> 'ACTIVE' OR (client_address IS NOT NULL AND client_public_key IS NOT NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    incompatible = connection.execute(
        sa.text("""SELECT EXISTS (
        SELECT 1 FROM vpn_credentials WHERE client_address IS NULL OR client_public_key IS NULL
            OR (source = 'ISSUED' AND encrypted_config IS NULL)
        UNION ALL SELECT 1 FROM vpn_credentials WHERE revoked_at IS NULL
            GROUP BY access_grant_id HAVING count(*) > 1
        UNION ALL SELECT 1 FROM vpn_credentials WHERE state IN ('RESERVED','PROVISIONING','REVOKING')
    )""")
    ).scalar()
    if incompatible:
        raise RuntimeError("Cannot downgrade gateway devices: finish cleanup and revoke additional devices first")
    unfinished = connection.execute(
        sa.text("""SELECT EXISTS (SELECT 1 FROM jobs
        WHERE (type::text IN ('CREATE_DEVICE','REVOKE_DEVICE') OR
            (type::text IN ('CREATE_USER','REVOKE_ACCESS') AND params ? 'credential_id'))
            AND status::text IN ('PENDING','RUNNING'))""")
    ).scalar()
    if unfinished:
        raise RuntimeError("Cannot downgrade with unfinished device jobs")
    # Preserve completed history in the vocabulary understood by the old application.
    op.execute("UPDATE jobs SET type = 'CREATE_USER' WHERE type::text = 'CREATE_DEVICE'")
    op.execute("UPDATE jobs SET type = 'REVOKE_ACCESS' WHERE type::text = 'REVOKE_DEVICE'")
    op.drop_constraint("ck_vpn_credentials_active_identity", "vpn_credentials", type_="check")
    op.drop_constraint("ck_vpn_credentials_issued_has_config", "vpn_credentials", type_="check")
    op.create_check_constraint(
        "ck_vpn_credentials_issued_has_config", "vpn_credentials", "source <> 'ISSUED' OR encrypted_config IS NOT NULL"
    )
    op.alter_column("vpn_credentials", "client_address", existing_type=sa.String(64), nullable=False)
    op.alter_column("vpn_credentials", "client_public_key", existing_type=sa.String(64), nullable=False)
    op.drop_index("uq_vpn_credentials_live_per_grant", table_name="vpn_credentials")
    op.create_index(
        "uq_vpn_credentials_live_per_grant",
        "vpn_credentials",
        ["access_grant_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_constraint("uq_vpn_credentials_request_id", "vpn_credentials", type_="unique")
    for column in ("request_id", "peer_name", "device_name", "is_gateway", "state"):
        op.drop_column("vpn_credentials", column)
    # Unused enum labels are harmless; no rows returned to the old ORM contain them.
