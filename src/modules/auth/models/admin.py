#!/usr/bin/python
# -*- coding: utf-8 -*-
from sqlalchemy import String, true
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class Admin(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "admins"
    email: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(server_default=true())
