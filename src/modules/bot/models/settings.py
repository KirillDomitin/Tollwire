#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, SmallInteger, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class BotSettings(Base):
    """Настройки бота: одна строка (id = 1). Значения по умолчанию для приглашений и порог «панель недоступна»."""

    __tablename__ = "bot_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_bot_settings_single_row"),
        CheckConstraint("default_days BETWEEN 1 AND 3650", name="ck_bot_settings_default_days"),
        CheckConstraint("invite_valid_days BETWEEN 1 AND 365", name="ck_bot_settings_invite_valid_days"),
        CheckConstraint(
            "panel_down_after_seconds BETWEEN 60 AND 86400", name="ck_bot_settings_panel_down_after_seconds"
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    default_protocol_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("protocols.id", ondelete="SET NULL"), default=None
    )
    default_days: Mapped[int] = mapped_column(server_default=text("30"), default=30)
    invite_valid_days: Mapped[int] = mapped_column(server_default=text("7"), default=7)
    panel_down_after_seconds: Mapped[int] = mapped_column(server_default=text("300"), default=300)  # читает 25.5
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("admins.id", ondelete="SET NULL"), default=None)
