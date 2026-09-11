#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin
from modules.access.models import AccessGrantStatus


class AccessGrant(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "access_grants"
    subscription_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("subscriptions.id", ondelete="CASCADE"))
    server_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_servers.id", ondelete="CASCADE"))
    protocol_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("protocols.id", ondelete="CASCADE"))
    status: Mapped[AccessGrantStatus] = mapped_column(
        default=AccessGrantStatus.PENDING, server_default=text("'PENDING'")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
