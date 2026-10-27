#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class ConfigLink(UuidPkMixin, Base):
    """Ссылка на страницу конфига клиента. Токен виден один раз, при выпуске; в БД только SHA-256.

    Ссылка привязана к клиенту (credential), а не к гранту: после перевыпуска ключа старая ссылка умирает вместе со
    старым клиентом. Действительность (клиент жив, доступ в силе, срок не вышел) проверяется при каждом открытии;
    `revoked_at` ставится только когда ссылку заменяет новая."""

    __tablename__ = "config_links"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_config_links_token_hash"),
        # Живая ссылка у клиента одна: новая отзывает прежнюю в той же транзакции, индекс страхует от гонки.
        Index(
            "uq_config_links_live_per_credential",
            "credential_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    token_hash: Mapped[str] = mapped_column(String(64))
    credential_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_credentials.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
