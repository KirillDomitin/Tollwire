#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class AdminTelegramLink(UuidPkMixin, Base):
    """Привязка Telegram админа к панели (этап 25.5): кому и о чём бот пишет уведомления.

    Код привязки - одноразовый, в БД только SHA-256. `events` - объект {ключ каталога: включено}."""

    __tablename__ = "admin_telegram_links"
    __table_args__ = (
        CheckConstraint("(telegram_id IS NULL) = (linked_at IS NULL)", name="ck_admin_telegram_links_linked"),
        CheckConstraint("(code_hash IS NULL) = (code_expires_at IS NULL)", name="ck_admin_telegram_links_code"),
    )

    admin_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("admins.id", ondelete="CASCADE"), unique=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, default=None)
    telegram_username: Mapped[str | None] = mapped_column(String(64), default=None)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    events: Mapped[dict[str, bool]] = mapped_column(JSONB)
    code_hash: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
