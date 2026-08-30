#!/usr/bin/python
# -*- coding: utf-8 -*-
from uuid import UUID

from redis.asyncio import Redis

from core.redis import get_redis

KEY: str = "jobs:queue"


class JobQueue:
    def __init__(self) -> None:
        self.client: Redis = get_redis()

    async def enqueue(self, job_id: UUID) -> None:
        await self.client.lpush(KEY, str(job_id))

    async def dequeue(self, timeout: int = 2) -> UUID | None:
        result = await self.client.brpop(KEY, timeout=timeout)
        if result is None:
            return None
        _, job_id = result
        return UUID(str(job_id))

    async def size(self) -> int:
        return await self.client.llen(KEY)
