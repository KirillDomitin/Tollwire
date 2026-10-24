#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class BotInvite(UuidPkMixin, Base):
    """Одноразовое приглашение: код показывается один раз, в БД - SHA-256. Протокол и срок доступа - в приглашении.

    «Кем использовано» берётся через регистрацию (bot_registrations.invite_id уникален), отдельного поля нет.
    """

    __tablename__ = "bot_invites"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_bot_invites_code_hash"),
        CheckConstraint("days BETWEEN 1 AND 3650", name="ck_bot_invites_days"),
    )

    code_hash: Mapped[str] = mapped_column(String(64))
    # SET NULL: удаление протокола не стирает историю приглашений; приглашение без протокола использовать нельзя.
    protocol_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("protocols.id", ondelete="SET NULL"), default=None
    )
    days: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(100), default=None)  # кому
    created_by: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("admins.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
