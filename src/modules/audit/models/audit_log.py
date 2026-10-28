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
    PASSWORD_CHANGED = "password_changed"  # админ сам сменил пароль; остальные его сессии завершены
    PASSWORD_CHANGE_FAILED = "password_change_failed"  # неверный текущий пароль при смене
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
    SERVER_DISCOVER_STARTED = "server_discover_started"
    SERVER_DISCOVER_COMPLETED = "server_discover_completed"
    SERVER_DISCOVER_FAILED = "server_discover_failed"
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
    CREDENTIAL_IMPORTED = "credential_imported"
    CREDENTIAL_ISSUE_FAILED = "credential_issue_failed"
    CREDENTIAL_REVOKED = "credential_revoked"
    CREDENTIAL_REVOKE_FAILED = "credential_revoke_failed"
    CONFIG_VIEWED = "config_viewed"
    # Импорт клиентов из установленной AmneziaVPN в БД панели
    CLIENTS_IMPORT_STARTED = "clients_import_started"
    CLIENTS_IMPORT_COMPLETED = "clients_import_completed"
    CLIENTS_IMPORT_FAILED = "clients_import_failed"
    # Выходы шлюза: подключение зарубежного сервера к шлюзу (задачи) и правка весов/пределов (без задачи)
    GATEWAY_EXIT_ADD_STARTED = "gateway_exit_add_started"
    GATEWAY_EXIT_ADD_COMPLETED = "gateway_exit_add_completed"
    GATEWAY_EXIT_ADD_FAILED = "gateway_exit_add_failed"
    GATEWAY_EXIT_REMOVE_STARTED = "gateway_exit_remove_started"
    GATEWAY_EXIT_REMOVE_COMPLETED = "gateway_exit_remove_completed"
    GATEWAY_EXIT_REMOVE_FAILED = "gateway_exit_remove_failed"
    GATEWAY_EXIT_UPDATED = "gateway_exit_updated"
    GATEWAY_CLIENT_ASSIGNED = "gateway_client_assigned"  # клиенту шлюза назначен выход (выдача, перенос)
    GATEWAY_CLIENT_UNASSIGNED = "gateway_client_unassigned"  # клиент остался без выхода (снят последний/нет места)
    # Россия напрямую (24.4)
    GEOIP_LIST_UPDATED = "geoip_list_updated"  # получен новый список (версия, число префиксов, sha256, статус)
    GEOIP_LIST_ACTIVATED = "geoip_list_activated"
    GEOIP_OVERRIDE_ADDED = "geoip_override_added"
    GEOIP_OVERRIDE_REMOVED = "geoip_override_removed"
    GATEWAY_RU_APPLIED = "gateway_ru_applied"  # список применён на шлюзе (число маршрутов)
    GATEWAY_RU_APPLY_FAILED = "gateway_ru_apply_failed"
    # Failover (24.5)
    GATEWAY_CLIENT_MOVED = "gateway_client_moved"  # клиент перенесён на другой выход (failover, возврат, вручную)
    GATEWAY_FAILOVER = "gateway_failover"  # выход отказал: сколько клиентов перенесено / осталось без выхода
    GATEWAY_ALL_EXITS_DOWN = (
        "gateway_all_exits_down"  # плохи все ACTIVE-выходы шлюза сразу (перезапуск / тотальный отказ)
    )
    GATEWAY_EXIT_RECOVERED = "gateway_exit_recovered"  # отказавший выход снова исправен
    GATEWAY_CONTAINER_RECREATED = "gateway_container_recreated"  # контейнер шлюза пересоздан (клиенты переподключаются)
    # Telegram-бот (25.1): ключи, приглашения, регистрации. В meta нет ключей, кодов, почты и telegram_id.
    BOT_KEY_CREATED = "bot_key_created"
    BOT_KEY_REVOKED = "bot_key_revoked"
    BOT_INVITE_CREATED = "bot_invite_created"
    BOT_INVITE_REVOKED = "bot_invite_revoked"
    BOT_REGISTRATION_CREATED = "bot_registration_created"  # приглашение занято, доступ выдаётся
    BOT_REGISTRATION_ACTIVATED = "bot_registration_activated"
    BOT_REGISTRATION_FAILED = "bot_registration_failed"  # выдача не удалась: источник уведомления админу (25.5)
    BOT_REGISTRATION_RETRIED = "bot_registration_retried"
    # Ссылка на страницу конфига (25.3): выпуск без токена и хеша; открытия в аудит не пишутся.
    CONFIG_LINK_CREATED = "config_link_created"
    # Страница «Бот» (25.4): рассылки, ссылка одному человеку в Telegram, настройки. В meta нет текста, telegram_id,
    # почты, токена и списков людей.
    BOT_BROADCAST_CREATED = "bot_broadcast_created"
    BOT_BROADCAST_CANCELLED = "bot_broadcast_cancelled"
    BOT_CONFIG_LINK_QUEUED = "bot_config_link_queued"
    BOT_SETTINGS_UPDATED = "bot_settings_updated"


class AuditEntity:
    """Значения entity_type: к какой сущности относится запись."""

    ADMIN = "admin"
    SERVER = "server"
    VPN_USER = "vpn_user"
    SUBSCRIPTION = "subscription"
    ACCESS_GRANT = "access_grant"
    VPN_CREDENTIAL = "vpn_credential"
    GEOIP = "geoip"
    BOT_KEY = "bot_key"
    BOT_INVITE = "bot_invite"
    BOT_REGISTRATION = "bot_registration"
    CONFIG_LINK = "config_link"
    BOT_BROADCAST = "bot_broadcast"
    BOT_SETTINGS = "bot_settings"


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
