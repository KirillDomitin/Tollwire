#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, SmallInteger, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class BotNotifyCursor(Base):
    """Курсор сборщика уведомлений по аудиту (этап 25.5): единственная строка `id = 1`."""

    __tablename__ = "bot_notify_cursor"
    __table_args__ = (CheckConstraint("id = 1", name="ck_bot_notify_cursor_single"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    cursor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BotNotifySeen(Base):
    """Записи аудита, уже просмотренные сборщиком (перекрытие курсора не даёт дублей). Без FK: аудит чистится."""

    __tablename__ = "bot_notify_seen"

    audit_log_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
