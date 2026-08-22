#!/usr/bin/python
# -*- coding: utf-8 -*-
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class AdminSession(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "admin_sessions"
    admin_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("admins.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(server_default=false())
