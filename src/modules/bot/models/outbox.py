#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, SmallInteger, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class OutboxKind(enum.StrEnum):
    NEWS = "news"
    CONFIG = "config"  # 25.5 добавит notify


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"  # выдано боту, ждём подтверждения
    SENT = "sent"
    BLOCKED = "blocked"  # человек заблокировал бота
    FAILED = "failed"
    SKIPPED = "skipped"  # отправлять нечего или некому (нет доступа, нет Telegram)
    CANCELLED = "cancelled"


OPEN_STATUSES = (OutboxStatus.PENDING, OutboxStatus.CLAIMED)

PRIORITY_INDIVIDUAL = 0  # «Отправить ссылку в Telegram» из карточки не ждёт большую рассылку
PRIORITY_BROADCAST = 10


class BotOutbox(UuidPkMixin, Base):
    """Исходящая очередь бота. Текста новости в строке нет (он в рассылке), токена ссылки на конфиг нет нигде: ссылка
    выпускается в момент выдачи сообщения боту и живёт только в ответе claim."""

    __tablename__ = "bot_outbox"
    __table_args__ = (
        CheckConstraint("kind IN ('news', 'config')", name="ck_bot_outbox_kind"),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'sent', 'blocked', 'failed', 'skipped', 'cancelled')",
            name="ck_bot_outbox_status",
        ),
        Index(
            "ix_bot_outbox_ready",
            "priority",
            "created_at",
            postgresql_where=text("status IN ('pending', 'claimed')"),
        ),
        Index("ix_bot_outbox_broadcast_status", "broadcast_id", "status"),
        Index("ix_bot_outbox_lease", "lease_id"),
        # Человек входит в рассылку один раз (NULL в broadcast_id - индивидуальные отправки - не считаются).
        Index("uq_bot_outbox_broadcast_user", "broadcast_id", "user_id", unique=True),
        # Две живые отправки ссылки одному человеку гасили бы друг друга: открытая одна.
        Index(
            "uq_bot_outbox_open_config",
            "user_id",
            unique=True,
            postgresql_where=text("kind = 'config' AND status IN ('pending', 'claimed')"),
        ),
    )

    kind: Mapped[str] = mapped_column(String(16))
    broadcast_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("bot_broadcasts.id", ondelete="CASCADE"), default=None
    )
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_users.id", ondelete="CASCADE"))
    # Доступ, по которому собрана рассылка «по протоколу»: ссылка выпускается на его клиента.
    access_grant_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("access_grants.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(String(16), default=OutboxStatus.PENDING.value)
    priority: Mapped[int] = mapped_column(SmallInteger, default=PRIORITY_BROADCAST)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    lease_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    claimed_by_key_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("bot_keys.id", ondelete="SET NULL"), default=None
    )
    attempts: Mapped[int] = mapped_column(server_default=text("0"), default=0)
    error: Mapped[str | None] = mapped_column(String(32), default=None)  # код из перечня, не текст Telegram
    config_link_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("config_links.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
