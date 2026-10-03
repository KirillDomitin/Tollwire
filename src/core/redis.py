#!/usr/bin/python
# -*- coding: utf-8 -*-

import redis.asyncio as aioredis
from redis.asyncio import Redis

from core.config import settings

# connect timeout — не висеть на недоступном Redis; health_check_interval — проверять «уснувшее» соединение
# (после перезапуска Redis) перед использованием, а не узнавать о разрыве посреди BRPOP.
client = aioredis.from_url(
    settings.redis_url, decode_responses=True, socket_connect_timeout=5, health_check_interval=30
)


def get_redis() -> Redis:
    return client
