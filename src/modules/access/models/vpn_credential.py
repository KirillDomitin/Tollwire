#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class VPNCredential(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "vpn_credentials"
    # История перевыпусков сохраняется, но живой (не отозванный) credential у гранта только один.
    __table_args__ = (
        Index(
            "uq_vpn_credentials_live_per_grant",
            "access_grant_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    access_grant_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("access_grants.id", ondelete="CASCADE"))
    encrypted_config: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
