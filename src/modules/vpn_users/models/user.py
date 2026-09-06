#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class VPNUserStatus(enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class VPNUser(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "vpn_users"
    username: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[VPNUserStatus] = mapped_column(default=VPNUserStatus.DISABLED, server_default=text("'DISABLED'"))
