"""admin notify: admin_telegram_links, bot_notify_cursor/seen, bot_outbox notify, bot_keys.last_seen_local

Revision ID: e7a4c1d92b58
Revises: c6e2f9a41d37
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7a4c1d92b58"
down_revision: Union[str, Sequence[str], None] = "c6e2f9a41d37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_telegram_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("admin_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(telegram_id IS NULL) = (linked_at IS NULL)", name="ck_admin_telegram_links_linked"),
        sa.CheckConstraint("(code_hash IS NULL) = (code_expires_at IS NULL)", name="ck_admin_telegram_links_code"),
        sa.ForeignKeyConstraint(
            ["admin_id"], ["admins.id"], name="admin_telegram_links_admin_id_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="admin_telegram_links_pkey"),
        sa.UniqueConstraint("admin_id", name="admin_telegram_links_admin_id_key"),
        sa.UniqueConstraint("telegram_id", name="admin_telegram_links_telegram_id_key"),
        sa.UniqueConstraint("code_hash", name="admin_telegram_links_code_hash_key"),
    )

    op.create_table(
        "bot_notify_cursor",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("cursor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_bot_notify_cursor_single"),
        sa.PrimaryKeyConstraint("id", name="bot_notify_cursor_pkey"),
    )
    op.execute("INSERT INTO bot_notify_cursor (id, cursor_at, started_at) VALUES (1, now(), now())")

    op.create_table(
        "bot_notify_seen",
        sa.Column("audit_log_id", sa.Uuid(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("audit_log_id", name="bot_notify_seen_pkey"),
    )

    op.add_column("bot_keys", sa.Column("last_seen_local", sa.Boolean(), nullable=True))

    op.alter_column("bot_outbox", "user_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("bot_outbox", sa.Column("admin_link_id", sa.Uuid(), nullable=True))
    op.add_column("bot_outbox", sa.Column("text", sa.Text(), nullable=True))
    op.add_column("bot_outbox", sa.Column("format", sa.String(length=8), nullable=True))
    op.add_column("bot_outbox", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bot_outbox", sa.Column("notify_key", sa.String(length=32), nullable=True))
    op.create_foreign_key(
        "bot_outbox_admin_link_id_fkey",
        "bot_outbox",
        "admin_telegram_links",
        ["admin_link_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_bot_outbox_admin_link", "bot_outbox", ["admin_link_id"])
    op.drop_constraint("ck_bot_outbox_kind", "bot_outbox", type_="check")
    op.create_check_constraint("ck_bot_outbox_kind", "bot_outbox", "kind IN ('news', 'config', 'notify')")
    op.create_check_constraint(
        "ck_bot_outbox_recipient",
        "bot_outbox",
        "(kind = 'notify' AND admin_link_id IS NOT NULL AND user_id IS NULL AND text IS NOT NULL "
        "AND format IS NOT NULL) OR (kind <> 'notify' AND user_id IS NOT NULL AND admin_link_id IS NULL)",
    )


def downgrade() -> None:
    # Привязки, курсор и очередь уведомлений теряются; строки notify удаляются до возврата NOT NULL на user_id.
    op.execute("DELETE FROM bot_outbox WHERE kind = 'notify'")
    op.drop_constraint("ck_bot_outbox_recipient", "bot_outbox", type_="check")
    op.drop_constraint("ck_bot_outbox_kind", "bot_outbox", type_="check")
    op.create_check_constraint("ck_bot_outbox_kind", "bot_outbox", "kind IN ('news', 'config')")
    op.drop_index("ix_bot_outbox_admin_link", table_name="bot_outbox")
    op.drop_constraint("bot_outbox_admin_link_id_fkey", "bot_outbox", type_="foreignkey")
    for column in ("notify_key", "expires_at", "format", "text", "admin_link_id"):
        op.drop_column("bot_outbox", column)
    op.alter_column("bot_outbox", "user_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_column("bot_keys", "last_seen_local")
    op.drop_table("bot_notify_seen")
    op.drop_table("bot_notify_cursor")
    op.drop_table("admin_telegram_links")
