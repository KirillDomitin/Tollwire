#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin
from modules.access.models import AccessGrantStatus


class Subscription(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_users.id", ondelete="CASCADE"))
    status: Mapped[AccessGrantStatus] = mapped_column(
        default=AccessGrantStatus.PENDING, server_default=text("'PENDING'")
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
