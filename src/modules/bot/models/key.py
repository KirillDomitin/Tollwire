#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class BotKey(UuidPkMixin, Base):
    """Ключ бота: открытое значение показывается один раз при создании, в БД - только SHA-256."""

    __tablename__ = "bot_keys"
    __table_args__ = (UniqueConstraint("key_hash", name="uq_bot_keys_key_hash"),)

    name: Mapped[str] = mapped_column(String(64))
    key_hash: Mapped[str] = mapped_column(String(64))
    key_prefix: Mapped[str] = mapped_column(String(8))  # первые символы: по ним в списке видно, какой это ключ
    created_by: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("admins.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_seen_version: Mapped[str | None] = mapped_column(String(32), default=None)
    bot_username: Mapped[str | None] = mapped_column(String(64), default=None)  # сообщает бот в heartbeat
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Когда бот последний раз забирал очередь сообщений: пусто или давно при непустой очереди - бот старой версии.
    last_claim_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
