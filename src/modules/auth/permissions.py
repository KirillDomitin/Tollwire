#!/usr/bin/python
# -*- coding: utf-8 -*-
"""RBAC: права и какие роли ими обладают. Каждый эндпоинт API требует ровно одно право (см. require)."""

import enum

from modules.auth.models.admin import AdminRole


class Permission(enum.Enum):
    SERVERS_READ = "servers:read"
    SERVERS_MANAGE = "servers:manage"  # создать/изменить/удалить, SSH-учётка, подключение, установка, ключ хоста
    SERVERS_CHECK = "servers:check"  # запустить sync / health
    VPN_USERS_READ = "vpn_users:read"
    VPN_USERS_MANAGE = "vpn_users:manage"
    ACCESS_READ = "access:read"  # подписки, доступы, метаданные VPN-клиентов
    ACCESS_MANAGE = "access:manage"  # выдать/изменить/отозвать, создать/отозвать VPN-клиента
    CONFIG_REVEAL = "config:reveal"  # показать VPN-конфиг: в нём приватный ключ клиента
    JOBS_READ = "jobs:read"
    AUDIT_READ = "audit:read"
    ADMINS_MANAGE = "admins:manage"


_VIEWER = frozenset(
    {
        Permission.SERVERS_READ,
        Permission.VPN_USERS_READ,
        Permission.ACCESS_READ,
        Permission.JOBS_READ,
        Permission.AUDIT_READ,
    }
)
_OPERATOR = _VIEWER | {
    Permission.SERVERS_CHECK,
    Permission.VPN_USERS_MANAGE,
    Permission.ACCESS_MANAGE,
    Permission.CONFIG_REVEAL,
}

ROLE_PERMISSIONS: dict[AdminRole, frozenset[Permission]] = {
    AdminRole.VIEWER: _VIEWER,
    AdminRole.OPERATOR: frozenset(_OPERATOR),
    AdminRole.SUPERADMIN: frozenset(Permission),
}


def has_permission(role: AdminRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
