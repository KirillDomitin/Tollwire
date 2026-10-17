#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum


class AccessGrantStatus(enum.Enum):
    ACTIVE = "active"
    PENDING = "pending"
    EXPIRED = "expired"
    REVOKED = "revoked"


class CredentialSource(enum.Enum):
    ISSUED = "issued"  # клиента создала панель: в БД есть его конфиг
    IMPORTED = "imported"  # клиент создан в приложении AmneziaVPN и подхвачен панелью: конфига (приватного ключа) нет
