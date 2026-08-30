#!/usr/bin/python
# -*- coding: utf-8 -*-

import redis.asyncio as aioredis
from redis.asyncio import Redis

from core.config import settings

client = aioredis.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> Redis:
    return client
