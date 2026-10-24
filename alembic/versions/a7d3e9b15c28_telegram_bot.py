"""telegram bot: keys, invites, registrations, vpn_users telegram fields

Revision ID: a7d3e9b15c28
Revises: f3b8d2a6c914
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a7d3e9b15c28"
down_revision: Union[str, Sequence[str], None] = "f3b8d2a6c914"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vpn_users", sa.Column("telegram_id", sa.BigInteger(), nullable=True))
    op.add_column("vpn_users", sa.Column("telegram_username", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_vpn_users_telegram_id", "vpn_users", ["telegram_id"])

    op.create_table(
        "bot_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=8), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_version", sa.String(length=32), nullable=True),
        sa.Column("bot_username", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["admins.id"], name="bot_keys_created_by_fkey", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_bot_keys_key_hash"),
    )
    op.create_table(
        "bot_invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("protocol_id", sa.Uuid(), nullable=True),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=100), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("days BETWEEN 1 AND 3650", name="ck_bot_invites_days"),
        sa.ForeignKeyConstraint(["created_by"], ["admins.id"], name="bot_invites_created_by_fkey", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["protocol_id"], ["protocols.id"], name="bot_invites_protocol_id_fkey", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash", name="uq_bot_invites_code_hash"),
    )
    op.create_table(
        "bot_registrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("invite_id", sa.Uuid(), nullable=False),
        sa.Column("bot_key_id", sa.Uuid(), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("access_grant_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["access_grant_id"],
            ["access_grants.id"],
            name="bot_registrations_access_grant_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["bot_key_id"], ["bot_keys.id"], name="bot_registrations_bot_key_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["invite_id"], ["bot_invites.id"], name="bot_registrations_invite_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["vpn_users.id"], name="bot_registrations_user_id_fkey", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_grant_id", name="uq_bot_registrations_access_grant_id"),
        sa.UniqueConstraint("invite_id", name="uq_bot_registrations_invite_id"),
    )
    op.create_index(
        "uq_bot_registrations_open_telegram_id",
        "bot_registrations",
        ["telegram_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PAYMENT_PENDING', 'PROVISIONING')"),
    )


def downgrade() -> None:
    # Связка пользователей с Telegram и все данные бота (ключи, приглашения, регистрации) теряются.
    op.drop_index(
        "uq_bot_registrations_open_telegram_id",
        table_name="bot_registrations",
        postgresql_where=sa.text("status IN ('PAYMENT_PENDING', 'PROVISIONING')"),
    )
    op.drop_table("bot_registrations")
    op.drop_table("bot_invites")
    op.drop_table("bot_keys")
    op.drop_constraint("uq_vpn_users_telegram_id", "vpn_users", type_="unique")
    op.drop_column("vpn_users", "telegram_username")
    op.drop_column("vpn_users", "telegram_id")
