#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class BroadcastKind(enum.StrEnum):
    NEWS = "news"  # текст админа (Telegram HTML)
    CONFIG = "config"  # каждому - новая ссылка на его конфиг


class BroadcastTarget(enum.StrEnum):
    ALL = "all"
    SELECTED = "selected"
    PROTOCOL = "protocol"


class BotBroadcast(UuidPkMixin, Base):
    """Рассылка: что и кому. Сами сообщения - строки bot_outbox; счётчики меняются в той же транзакции, что и статус
    строки, поэтому переживают чистку очереди. total = итоги + открытые строки + выбывшие (удалённые люди)."""

    __tablename__ = "bot_broadcasts"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_bot_broadcasts_request_id"),
        CheckConstraint("kind IN ('news', 'config')", name="ck_bot_broadcasts_kind"),
        CheckConstraint("target IN ('all', 'selected', 'protocol')", name="ck_bot_broadcasts_target"),
        CheckConstraint("kind = 'config' OR text IS NOT NULL", name="ck_bot_broadcasts_news_text"),
    )

    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text, default=None)  # только у news
    target: Mapped[str] = mapped_column(String(16))
    protocol_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("protocols.id", ondelete="SET NULL"), default=None
    )
    request_id: Mapped[UUID] = mapped_column(Uuid)  # повторный POST с тем же id возвращает ту же рассылку
    created_by: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("admins.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    total: Mapped[int] = mapped_column(server_default=sql_text("0"), default=0)
    sent: Mapped[int] = mapped_column(server_default=sql_text("0"), default=0)
    blocked: Mapped[int] = mapped_column(server_default=sql_text("0"), default=0)
    failed: Mapped[int] = mapped_column(server_default=sql_text("0"), default=0)
    skipped: Mapped[int] = mapped_column(server_default=sql_text("0"), default=0)
    cancelled: Mapped[int] = mapped_column(server_default=sql_text("0"), default=0)
