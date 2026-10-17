#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin
from modules.access.models import CredentialSource


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
        # Конфига нет только у импортированного клиента: приватный ключ остался в приложении на устройстве.
        CheckConstraint(
            "source <> 'ISSUED' OR encrypted_config IS NOT NULL", name="ck_vpn_credentials_issued_has_config"
        ),
    )

    access_grant_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("access_grants.id", ondelete="CASCADE"))
    encrypted_config: Mapped[str | None] = mapped_column(Text)
    # Не секрет: по публичному ключу клиента его снимают с сервера и сверяют сервер с БД (блок 13).
    client_address: Mapped[str] = mapped_column(String(64))
    client_public_key: Mapped[str] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # VARCHAR, а не Postgres-enum: новые источники добавляются без ALTER TYPE (как AuditAction).
    source: Mapped[CredentialSource] = mapped_column(
        Enum(CredentialSource, native_enum=False, length=16),
        default=CredentialSource.ISSUED,
        server_default=text("'ISSUED'"),
    )
    # PSK подхваченного клиента (Fernet): нужен, чтобы sync вернул его peer на сервер. NULL — у клиента PSK не было.
    encrypted_psk: Mapped[str | None] = mapped_column(Text, default=None)
    # Имя клиента в приложении AmneziaVPN (из clientsTable), для показа админу. Не секрет.
    external_name: Mapped[str | None] = mapped_column(String(100), default=None)
