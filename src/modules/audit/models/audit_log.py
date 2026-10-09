#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class AuditAction(enum.Enum):
    # Вход/выход админа
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGIN_LOCKED = "login_locked"  # учётка закрыта для входа после серии неудач (rate limiting)
    LOGOUT = "logout"
    # Админы панели (RBAC)
    ADMIN_CREATED = "admin_created"
    ADMIN_UPDATED = "admin_updated"  # смена роли / отключение / включение
    # Серверы
    SERVER_CREATED = "server_created"
    SERVER_UPDATED = "server_updated"
    SERVER_DELETED = "server_deleted"
    SERVER_CREDENTIALS_SET = "server_credentials_set"
    SERVER_CREDENTIALS_DELETED = "server_credentials_deleted"
    SERVER_HOST_KEY_PINNED = "server_host_key_pinned"  # ключ SSH-сервера закреплён (TOFU или вручную)
    SERVER_HOST_KEY_RESET = "server_host_key_reset"
    SERVER_HOST_KEY_MISMATCH = "server_host_key_mismatch"  # сервер предъявил другой ключ: возможна подмена
    SERVER_INSTALL_STARTED = "server_install_started"
    SERVER_INSTALL_COMPLETED = "server_install_completed"
    SERVER_INSTALL_FAILED = "server_install_failed"
    SERVER_SYNC_STARTED = "server_sync_started"
    SERVER_SYNC_COMPLETED = "server_sync_completed"
    SERVER_SYNC_FAILED = "server_sync_failed"
    SERVER_SSH_KEY_INSTALL_STARTED = "server_ssh_key_install_started"  # панель ставит свой SSH-ключ вместо пароля
    SERVER_SSH_KEY_INSTALL_COMPLETED = "server_ssh_key_install_completed"
    SERVER_SSH_KEY_INSTALL_FAILED = "server_ssh_key_install_failed"
    # Протоколы
    PROTOCOL_INSTALL_STARTED = "protocol_install_started"
    PROTOCOL_INSTALL_COMPLETED = "protocol_install_completed"
    PROTOCOL_INSTALL_FAILED = "protocol_install_failed"
    PROTOCOL_REMOVE_STARTED = "protocol_remove_started"
    PROTOCOL_REMOVE_COMPLETED = "protocol_remove_completed"
    PROTOCOL_REMOVE_FAILED = "protocol_remove_failed"
    # VPN-пользователи
    VPN_USER_CREATED = "vpn_user_created"
    VPN_USER_UPDATED = "vpn_user_updated"
    VPN_USER_DELETED = "vpn_user_deleted"
    # Подписки
    SUBSCRIPTION_CREATED = "subscription_created"
    SUBSCRIPTION_UPDATED = "subscription_updated"
    SUBSCRIPTION_ACTIVATED = "subscription_activated"
    SUBSCRIPTION_REVOKED = "subscription_revoked"
    SUBSCRIPTION_EXPIRED = "subscription_expired"
    SUBSCRIPTION_DELETED = "subscription_deleted"
    # Доступы (гранты)
    ACCESS_GRANTED = "access_granted"
    ACCESS_UPDATED = "access_updated"
    ACCESS_REVOKED = "access_revoked"
    ACCESS_EXPIRED = "access_expired"
    ACCESS_DELETED = "access_deleted"
    # Клиенты на серверах (credential'ы)
    CREDENTIAL_ISSUED = "credential_issued"
    CREDENTIAL_ISSUE_FAILED = "credential_issue_failed"
    CREDENTIAL_REVOKED = "credential_revoked"
    CREDENTIAL_REVOKE_FAILED = "credential_revoke_failed"
    CONFIG_VIEWED = "config_viewed"


class AuditEntity:
    """Значения entity_type: к какой сущности относится запись."""

    ADMIN = "admin"
    SERVER = "server"
    VPN_USER = "vpn_user"
    SUBSCRIPTION = "subscription"
    ACCESS_GRANT = "access_grant"
    VPN_CREDENTIAL = "vpn_credential"


class AuditLog(UuidPkMixin, Base):
    """Журнал критических действий. Только дописывается: ни изменения, ни удаления через API.

    entity_id — без внешнего ключа: запись должна пережить удаление сущности.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        # Курсорная пагинация журнала: порядок (created_at DESC, id DESC).
        Index("ix_audit_logs_created_at_id", text("created_at DESC"), text("id DESC")),
    )

    admin_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("admins.id", ondelete="SET NULL"), index=True, default=None
    )
    # VARCHAR, а не Postgres-enum: новые действия добавляются без миграции ALTER TYPE.
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction, native_enum=False, length=64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), default=None)
    entity_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    # Атрибут meta, колонка metadata: имя metadata у ORM-моделей занято (Base.metadata).
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, default=None)
    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
