#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class VPNUserStatus(enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class VPNUser(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "vpn_users"
    __table_args__ = (UniqueConstraint("telegram_id", name="uq_vpn_users_telegram_id"),)
    username: Mapped[str] = mapped_column(String(100))
    # NULL — у клиентов, подхваченных из AmneziaVPN (там email не было); unique допускает несколько NULL.
    email: Mapped[str | None] = mapped_column(String(100), unique=True)
    # Заполнены у пользователей, пришедших через Telegram-бота; у созданных админом или импортом — NULL.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    telegram_username: Mapped[str | None] = mapped_column(String(64), default=None)  # для показа, может меняться
    # Telegram ответил, что человек заблокировал бота: рассылки его пропускают, пока он сам не напишет боту.
    bot_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    description: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[VPNUserStatus] = mapped_column(default=VPNUserStatus.DISABLED, server_default=text("'DISABLED'"))
