#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Ограничение частоты (rate limiting) на Redis: счётчик в фиксированном окне.

Ключ ratelimit:<правило>:<субъект> живёт ровно одно окно (EXPIRE ставится при первом попадании).
Redis общий для всех копий api, поэтому лимит общий, а не «на процесс».

Если Redis недоступен — пропускаем (fail-open) с предупреждением в лог: без Redis панель и так
наполовину не работает (очередь задач), а грубый лимит в nginx продолжает действовать.
"""

import hashlib
import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.exceptions import AppError
from core.redis import get_redis

logger = logging.getLogger("rate_limit")


@dataclass(frozen=True)
class Limit:
    name: str
    max_hits: int
    window_seconds: int


class RateLimitExceeded(AppError):
    def __init__(self, limit: Limit, retry_after: int):
        super().__init__(f"Rate limit '{limit.name}' exceeded")
        self.limit = limit
        self.retry_after = max(retry_after, 1)


def subject_key(value: str) -> str:
    """Email и т.п. в ключ Redis кладём хэшем: в Redis не остаются адреса в открытом виде."""
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:32]


class RateLimiter:
    def __init__(self, redis: Redis | None = None):
        self.redis = redis or get_redis()

    @staticmethod
    def _key(limit: Limit, subject: str) -> str:
        return f"ratelimit:{limit.name}:{subject}"

    async def hit(self, limit: Limit, subject: str) -> None:
        """Засчитать попытку; если она сверх лимита — RateLimitExceeded."""
        count, ttl = await self._incr(limit, subject)
        if count > limit.max_hits:
            self._exceeded(limit, ttl)

    async def ensure_not_exceeded(self, limit: Limit, subject: str) -> None:
        """Проверить без засчитывания (для блокировки по неудачам: считаем только провалы)."""
        try:
            key = self._key(limit, subject)
            raw = await self.redis.get(key)
            ttl = await self.redis.ttl(key) if raw is not None else 0
        except RedisError as exc:
            logger.warning(
                "rate limit check skipped: redis unavailable", extra={"limit": limit.name, "error": str(exc)}
            )
            return
        if raw is not None and int(raw) >= limit.max_hits:
            self._exceeded(limit, ttl)

    async def record(self, limit: Limit, subject: str) -> int:
        """Засчитать событие (например, неудачный вход). Возвращает текущее число в окне (0 — Redis недоступен)."""
        count, _ = await self._incr(limit, subject)
        return count

    async def reset(self, limit: Limit, subject: str) -> None:
        try:
            await self.redis.delete(self._key(limit, subject))
        except RedisError as exc:
            logger.warning(
                "rate limit reset skipped: redis unavailable", extra={"limit": limit.name, "error": str(exc)}
            )

    async def _incr(self, limit: Limit, subject: str) -> tuple[int, int]:
        key = self._key(limit, subject)
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, limit.window_seconds, nx=True)  # окно начинается с первого попадания
                pipe.ttl(key)
                count, _, ttl = await pipe.execute()
        except RedisError as exc:
            logger.warning("rate limit skipped: redis unavailable", extra={"limit": limit.name, "error": str(exc)})
            return 0, 0
        return int(count), int(ttl)

    @staticmethod
    def _exceeded(limit: Limit, ttl: int) -> None:
        logger.warning("rate limit exceeded", extra={"limit": limit.name})
        raise RateLimitExceeded(limit, ttl if ttl > 0 else limit.window_seconds)
