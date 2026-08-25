#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class AuthType(enum.Enum):
    PASSWORD = "password"
    SSH_KEY = "ssh_key"


class ServerCredential(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "server_credentials"
    server_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_servers.id", ondelete="CASCADE"), unique=True)
    auth_type: Mapped[AuthType] = mapped_column(default=AuthType.PASSWORD, server_default=text("'PASSWORD'"))
    username: Mapped[str] = mapped_column(String(50), default="root")
    secret_encrypted: Mapped[str] = mapped_column(Text)
