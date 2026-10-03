#!/usr/bin/python
# -*- coding: utf-8 -*-

import enum
from datetime import datetime

from sqlalchemy import DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class ServerStatus(enum.Enum):
    NEW = "new"
    INSTALLING = "installing"
    READY = "ready"
    OFFLINE = "offline"
    ERROR = "error"


class HealthStatus(enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"  # SSH доступен, но часть проверок не прошла
    DOWN = "down"  # до сервера не достучаться


class VPNServer(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "vpn_servers"
    name: Mapped[str] = mapped_column(unique=True)
    host: Mapped[str] = mapped_column(String(100))
    ssh_port: Mapped[int] = mapped_column(server_default=text("22"))
    status: Mapped[ServerStatus] = mapped_column(default=ServerStatus.NEW, server_default=text("'NEW'"))
    description: Mapped[str | None] = mapped_column(String(300))
    # Сводка последнего health-чека; полный отчёт — в Job.result задачи HEALTH_CHECK.
    health_status: Mapped[HealthStatus | None] = mapped_column(default=None)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Закреплённый ключ SSH-сервера (host key) — публичный, не секрет. NULL — ещё не закреплён: при следующем
    # подключении ключ запомнится (TOFU). Подключение с другим ключом отклоняется до отправки пароля.
    ssh_host_key: Mapped[str | None] = mapped_column(Text, default=None)
    ssh_host_key_fingerprint: Mapped[str | None] = mapped_column(String(100), default=None)
