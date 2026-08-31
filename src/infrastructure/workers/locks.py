#!/usr/bin/python
# -*- coding: utf-8 -*-
from uuid import UUID

from redis.asyncio.lock import Lock

from core.exceptions import ServerBusyError
from core.redis import get_redis


class ServerLock:
    def __init__(self, server_id: UUID, timeout: int = 300, blocking_timeout: int = 5):
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
        if self.lock:
            await self.lock.release()

    def build(self) -> Lock:
        return self.client.lock(
            f"lock:server:{self.server_id}", timeout=self.timeout, blocking_timeout=self.blocking_timeout
        )
