#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class RegistrationStatus(enum.Enum):
    PAYMENT_PENDING = "payment_pending"  # ждёт оплаты (политика оплаты ответила «ещё нет»)
    PROVISIONING = "provisioning"  # пользователь, подписка и грант созданы, задача CREATE_USER выдаёт клиента
    ACTIVE = "active"
    FAILED = "failed"  # выдача не удалась; админ повторяет


# Статусы «в работе»: на один telegram_id одна такая регистрация.
OPEN_STATUSES = (RegistrationStatus.PAYMENT_PENDING, RegistrationStatus.PROVISIONING)


class BotRegistration(UuidPkMixin, Base):
    __tablename__ = "bot_registrations"
    __table_args__ = (
        UniqueConstraint("invite_id", name="uq_bot_registrations_invite_id"),
        UniqueConstraint("access_grant_id", name="uq_bot_registrations_access_grant_id"),
        Index(
            "uq_bot_registrations_open_telegram_id",
            "telegram_id",
            unique=True,
            postgresql_where=text("status IN ('PAYMENT_PENDING', 'PROVISIONING')"),
        ),
    )

    # Приглашение одноразовое (UNIQUE): вторую регистрацию с ним БД не пропустит, даже если проверки сервиса обошли.
    invite_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("bot_invites.id", ondelete="CASCADE"))
    bot_key_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("bot_keys.id", ondelete="SET NULL"), default=None)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    telegram_username: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(100))
    status: Mapped[RegistrationStatus] = mapped_column(Enum(RegistrationStatus, native_enum=False, length=16))
    user_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("vpn_users.id", ondelete="SET NULL"), default=None)
    # Задачу выдачи находим по гранту (jobs.access_grant_id): job_id не храним, задачи чистятся через 90 дней.
    access_grant_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("access_grants.id", ondelete="SET NULL"), default=None
    )
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
