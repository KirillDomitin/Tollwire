#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from uuid import UUID

from redis.asyncio.lock import Lock
from redis.exceptions import LockError

from core.config import settings
from core.exceptions import ServerBusyError
from core.redis import get_redis

logger = logging.getLogger("worker.lock")


class ServerLock:
    """Одна задача на сервер за раз (Redis lock с TTL — переживает падение воркера).

    TTL — не меньше самой долгой задачи: установка с docker pull идёт до ~10 мин. Если бы ключ истёк раньше,
    другой воркер взял бы тот же сервер посреди установки (гонка на awg0.conf). Берём таймаут «зависшей»
    задачи: дольше неё задача и так считается мёртвой и переводится в FAILED (правило stale_jobs).
    """

    def __init__(self, server_id: UUID, timeout: int = settings.stale_job_timeout_seconds, blocking_timeout: int = 5):
        self.server_id = server_id
        self.timeout = timeout
        self.blocking_timeout = blocking_timeout
        self.client = get_redis()
        self.lock: Lock | None = None

    async def __aenter__(self) -> Lock:
        self.lock = self.build()
        acquired = await self.lock.acquire()
        if not acquired:
            raise ServerBusyError("Lock acquisition failed: resource is currently held by another process")
        return self.lock

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if not self.lock:
            return
        try:
            await self.lock.release()
        except LockError:
            # Ключ уже истёк (или взят другим). Работа сделана — не превращаем успех задачи в FAILED и не
            # затираем исходное исключение. Но это сигнал: задача шла дольше TTL.
            logger.warning("server lock expired before release", extra={"server_id": str(self.server_id)})

    def build(self) -> Lock:
        return self.client.lock(
            f"lock:server:{self.server_id}", timeout=self.timeout, blocking_timeout=self.blocking_timeout
        )
