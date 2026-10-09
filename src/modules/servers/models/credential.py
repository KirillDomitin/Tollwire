#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text, Uuid, false, text
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
    # Ключ сгенерирован панелью и поставлен ею на сервер (задача INSTALL_SSH_KEY); ручная смена учётки — False.
    managed_key: Mapped[bool] = mapped_column(default=False, server_default=false())
    public_key: Mapped[str | None] = mapped_column(Text, default=None)  # строка OpenSSH ключа панели, не секрет
    key_fingerprint: Mapped[str | None] = mapped_column(String(100), default=None)  # SHA256:… для любого ключа
