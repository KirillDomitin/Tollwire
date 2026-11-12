#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin
from modules.access.models import CredentialSource, CredentialState


class VPNCredential(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "vpn_credentials"
    # История перевыпусков сохраняется, но живой (не отозванный) credential у гранта только один.
    __table_args__ = (
        Index(
            "uq_vpn_credentials_live_per_grant",
            "access_grant_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND NOT is_gateway"),
        ),
        # Конфига нет только у импортированного клиента: приватный ключ остался в приложении на устройстве.
        CheckConstraint(
            "state <> 'ACTIVE' OR source <> 'ISSUED' OR encrypted_config IS NOT NULL",
            name="ck_vpn_credentials_issued_has_config",
        ),
        CheckConstraint("state <> 'ACTIVE' OR (client_address IS NOT NULL AND client_public_key IS NOT NULL)",
                        name="ck_vpn_credentials_active_identity"),
    )

    access_grant_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("access_grants.id", ondelete="CASCADE"))
    encrypted_config: Mapped[str | None] = mapped_column(Text)
    # Не секрет: по публичному ключу клиента его снимают с сервера и сверяют сервер с БД (блок 13).
    client_address: Mapped[str | None] = mapped_column(String(64))
    client_public_key: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[CredentialState] = mapped_column(
        Enum(CredentialState, native_enum=False, length=16), default=CredentialState.ACTIVE,
        server_default=text("'ACTIVE'"),
    )
    is_gateway: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    device_name: Mapped[str | None] = mapped_column(String(100), default=None)
    peer_name: Mapped[str | None] = mapped_column(String(64), default=None)
    request_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True, default=None)

    @property
    def has_config(self) -> bool:
        return self.state == CredentialState.ACTIVE and self.revoked_at is None and self.encrypted_config is not None
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
