#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime

from sqlalchemy import DateTime, String, true
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class AdminRole(enum.Enum):
    """Роли админов панели. Что разрешено каждой — в modules/auth/permissions.py."""

    SUPERADMIN = "superadmin"  # всё: инфраструктура (серверы, SSH, установка) и управление админами
    OPERATOR = "operator"  # VPN-пользователи, доступы, выдача конфигов, sync/health
    VIEWER = "viewer"  # только чтение (без выдачи VPN-конфигов)


class Admin(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "admins"
    email: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(server_default=true())
    # По умолчанию — минимум прав: роль повыше выдаётся явно.
    role: Mapped[AdminRole] = mapped_column(default=AdminRole.VIEWER)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
