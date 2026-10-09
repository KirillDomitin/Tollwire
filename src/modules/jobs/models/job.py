#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class JobStatus(enum.Enum):
    FAILED = "failed"
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    CANCELLED = "cancelled"


class JobType(enum.Enum):
    CREATE_USER = "create_user"
    DELETE_USER = "delete_user"
    SYNC_SERVER = "sync_server"
    HEALTH_CHECK = "health_check"
    REVOKE_ACCESS = "revoke_access"
    INSTALL_SERVER = "install_server"
    REMOVE_PROTOCOL = "remove_protocol"
    UNINSTALL_SERVER = "uninstall_server"
    INSTALL_PROTOCOL = "install_protocol"
    INSTALL_SSH_KEY = "install_ssh_key"


class Job(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "jobs"
    type: Mapped[JobType] = mapped_column(default=JobType.HEALTH_CHECK, server_default=text("'HEALTH_CHECK'"))
    status: Mapped[JobStatus] = mapped_column(default=JobStatus.PENDING, server_default=text("'PENDING'"))
    server_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("vpn_servers.id", ondelete="SET NULL"))
    # Для задач по доступу (CREATE_USER, REVOKE_ACCESS): над каким грантом работать. SET NULL — история
    # задач переживает удаление гранта, как и в случае server_id.
    access_grant_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("access_grants.id", ondelete="SET NULL"), default=None
    )
    # Кто запустил задачу: воркер пишет аудит её результата от имени этого админа. NULL — система.
    admin_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("admins.id", ondelete="SET NULL"), default=None)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message: Mapped[str | None] = mapped_column(String(100), default=None)
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Итог задачи, если он есть (отчёт sync/health). Без секретов — поле отдаётся в JobRead как есть.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
