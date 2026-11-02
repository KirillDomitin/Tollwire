"""Shared Telegram pacing for the two background senders."""

import asyncio
import time
from collections import deque


class MessagePacer:
    def __init__(self, rate: int) -> None:
        self._rate = rate
        self._window: deque[float] = deque()
        self._chats: dict[int, float] = {}
        self._pause_until = 0.0
        self._lock = asyncio.Lock()

    def pause(self, seconds: float) -> None:
        self._pause_until = max(self._pause_until, time.monotonic() + seconds)

    async def acquire(self, chat: int) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._window and self._window[0] <= now - 1:
                    self._window.popleft()
                wait = max(
                    self._pause_until - now,
                    self._chats.get(chat, -float("inf")) + 1 - now,
                    self._window[0] + 1 - now if len(self._window) >= self._rate else 0,
                )
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
            self._window.append(now)
            self._chats[chat] = now
            if len(self._chats) > 10000:
                self._chats = {key: at for key, at in self._chats.items() if at > now - 1}
