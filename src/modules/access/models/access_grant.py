#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin
from modules.access.models import AccessGrantStatus


class AccessGrant(UuidPkMixin, TimestampMixin, Base):
    # Один живой грант на протокол в рамках подписки (а живая подписка у пользователя одна — значит, и на
    # пользователя). То же правило, что в сервисе, но без гонки двух одновременных запросов.
    __table_args__ = (
        Index(
            "uq_access_grants_live_per_protocol",
            "subscription_id",
            "protocol_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'ACTIVE')"),
        ),
    )

    __tablename__ = "access_grants"
    subscription_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("subscriptions.id", ondelete="CASCADE"))
    server_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_servers.id", ondelete="CASCADE"))
    protocol_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("protocols.id", ondelete="CASCADE"))
    status: Mapped[AccessGrantStatus] = mapped_column(
        default=AccessGrantStatus.PENDING, server_default=text("'PENDING'")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
