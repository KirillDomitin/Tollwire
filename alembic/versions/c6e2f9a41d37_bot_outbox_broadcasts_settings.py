"""bot outbox, broadcasts and settings; vpn_users.bot_blocked_at; bot_keys.last_claim_at

Revision ID: c6e2f9a41d37
Revises: b4f1c8e27a69
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c6e2f9a41d37"
down_revision: Union[str, Sequence[str], None] = "b4f1c8e27a69"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vpn_users", sa.Column("bot_blocked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bot_keys", sa.Column("last_claim_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "bot_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("default_protocol_id", sa.Uuid(), nullable=True),
        sa.Column("default_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("invite_valid_days", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column("panel_down_after_seconds", sa.Integer(), server_default=sa.text("300"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_bot_settings_single_row"),
        sa.CheckConstraint("default_days BETWEEN 1 AND 3650", name="ck_bot_settings_default_days"),
        sa.CheckConstraint("invite_valid_days BETWEEN 1 AND 365", name="ck_bot_settings_invite_valid_days"),
        sa.CheckConstraint(
            "panel_down_after_seconds BETWEEN 60 AND 86400", name="ck_bot_settings_panel_down_after_seconds"
        ),
        sa.ForeignKeyConstraint(
            ["default_protocol_id"],
            ["protocols.id"],
            name="fk_bot_settings_default_protocol_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["admins.id"], name="fk_bot_settings_updated_by", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO bot_settings (id) VALUES (1)")

    op.create_table(
        "bot_broadcasts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("protocol_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blocked", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cancelled", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("kind IN ('news', 'config')", name="ck_bot_broadcasts_kind"),
        sa.CheckConstraint("target IN ('all', 'selected', 'protocol')", name="ck_bot_broadcasts_target"),
        sa.CheckConstraint("kind = 'config' OR text IS NOT NULL", name="ck_bot_broadcasts_news_text"),
        sa.ForeignKeyConstraint(
            ["protocol_id"], ["protocols.id"], name="fk_bot_broadcasts_protocol_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["admins.id"], name="fk_bot_broadcasts_created_by", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_bot_broadcasts_request_id"),
    )

    op.create_table(
        "bot_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("broadcast_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("access_grant_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by_key_id", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.String(length=32), nullable=True),
        sa.Column("config_link_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('news', 'config')", name="ck_bot_outbox_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'sent', 'blocked', 'failed', 'skipped', 'cancelled')",
            name="ck_bot_outbox_status",
        ),
        sa.ForeignKeyConstraint(
            ["broadcast_id"], ["bot_broadcasts.id"], name="fk_bot_outbox_broadcast_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["vpn_users.id"], name="fk_bot_outbox_user_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["access_grant_id"], ["access_grants.id"], name="fk_bot_outbox_access_grant_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["claimed_by_key_id"], ["bot_keys.id"], name="fk_bot_outbox_claimed_by_key_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["config_link_id"], ["config_links.id"], name="fk_bot_outbox_config_link_id", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bot_outbox_ready",
        "bot_outbox",
        ["priority", "created_at"],
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )
    op.create_index("ix_bot_outbox_broadcast_status", "bot_outbox", ["broadcast_id", "status"])
    op.create_index("ix_bot_outbox_lease", "bot_outbox", ["lease_id"])
    op.create_index("uq_bot_outbox_broadcast_user", "bot_outbox", ["broadcast_id", "user_id"], unique=True)
    op.create_index(
        "uq_bot_outbox_open_config",
        "bot_outbox",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'config' AND status IN ('pending', 'claimed')"),
    )


def downgrade() -> None:
    # Теряются очередь сообщений, итоги рассылок, настройки бота и пометки «заблокировал бота».
    op.drop_index("uq_bot_outbox_open_config", table_name="bot_outbox")
    op.drop_index("uq_bot_outbox_broadcast_user", table_name="bot_outbox")
    op.drop_index("ix_bot_outbox_lease", table_name="bot_outbox")
    op.drop_index("ix_bot_outbox_broadcast_status", table_name="bot_outbox")
    op.drop_index("ix_bot_outbox_ready", table_name="bot_outbox")
    op.drop_table("bot_outbox")
    op.drop_table("bot_broadcasts")
    op.drop_table("bot_settings")
    op.drop_column("bot_keys", "last_claim_at")
    op.drop_column("vpn_users", "bot_blocked_at")
