#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Шаг оплаты регистрации. Сейчас сервис бесплатный по приглашениям (InviteFree); оплата подключается новой политикой,
остальные шаги регистрации не меняются."""

import enum
from typing import Protocol

from modules.bot.models.registration import BotRegistration


class PaymentStatus(enum.Enum):
    PAID = "paid"
    PENDING = "pending"  # регистрация ждёт оплаты (payment_pending)


class PaymentPolicy(Protocol):
    async def check(self, registration: BotRegistration) -> PaymentStatus: ...


class InviteFree:
    """Приглашение бесплатное: оплата всегда «пройдена»."""

    async def check(self, registration: BotRegistration) -> PaymentStatus:
        return PaymentStatus.PAID
