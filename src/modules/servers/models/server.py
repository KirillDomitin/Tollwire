#!/usr/bin/python
# -*- coding: utf-8 -*-

import enum

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class ServerStatus(enum.Enum):
    NEW = "new"
    INSTALLING = "installing"
    READY = "ready"
    OFFLINE = "offline"
    ERROR = "error"


class VPNServer(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "vpn_servers"
    name: Mapped[str] = mapped_column(unique=True)
    host: Mapped[str] = mapped_column(String(100))
    ssh_port: Mapped[int] = mapped_column(server_default=text("22"))
    status: Mapped[ServerStatus] = mapped_column(default=ServerStatus.NEW, server_default=text("'NEW'"))
    description: Mapped[str | None] = mapped_column(String(300))
