"""config links: tokens of the public config page

Revision ID: b4f1c8e27a69
Revises: a7d3e9b15c28
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b4f1c8e27a69"
down_revision: Union[str, Sequence[str], None] = "a7d3e9b15c28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "config_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["vpn_credentials.id"], name="fk_config_links_credential_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_config_links_token_hash"),
    )
    op.create_index(
        "uq_config_links_live_per_credential",
        "config_links",
        ["credential_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    # Все ссылки теряются: людям нужно снова написать боту /config.
    op.drop_index(
        "uq_config_links_live_per_credential",
        table_name="config_links",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("config_links")
