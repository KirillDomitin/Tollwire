#!/usr/bin/python
# -*- coding: utf-8 -*-
from core.config import settings
from core.rate_limit import Limit

# Неудачи авторизации (нет ключа, неверный, отозванный) по IP: считаются ДО поиска ключа в БД, перебор ключей упирается
# в этот порог. Ключ - 43 случайных символа, перебор и так безнадёжен; лимит держит нагрузку на БД.
BOT_AUTH_FAILURES_BY_IP = Limit(
    "bot_auth_ip_failures", settings.bot_auth_ip_max_failures, settings.bot_auth_ip_window_seconds
)
# Все запросы бота по ключу: бот опрашивает статус регистрации раз в 2-3 с, очередь - раз в 5 с, heartbeat - раз в 30 с.
BOT_REQUESTS_BY_KEY = Limit("bot_key_requests", settings.bot_key_max_requests, settings.bot_key_window_seconds)
# Регистрации - свой, более строгий лимит: каждая создаёт пользователя и задачу на реальном сервере.
BOT_REGISTRATIONS_BY_KEY = Limit(
    "bot_key_registrations", settings.bot_registrations_max, settings.bot_registrations_window_seconds
)
