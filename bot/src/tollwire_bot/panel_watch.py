"""Persistent panel liveness and per-recipient outage delivery. No panel imports or probes."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter

from tollwire_bot.pacing import MessagePacer
from tollwire_bot.panel import NotifyConfig
from tollwire_bot.storage import SqliteStorage
from tollwire_bot.texts import t

logger = logging.getLogger(__name__)
INTERVAL = 15


class PanelWatch:
    def __init__(
        self,
        bot: Bot,
        storage: SqliteStorage,
        *,
        repeat_seconds: int = 1800,
        pacer: MessagePacer | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bot, self._storage = bot, storage
        self._pacer = pacer
        self._repeat = max(1800, repeat_seconds)
        self._sleep, self._clock = sleep, clock
        self._last_ok = clock()
        self._threshold = 300
        self._chats: list[int] = []
        self._down: dict[int, float] = {}
        self._recovering: set[int] = set()
        self._last_send: dict[int, float] = {}
        self._empty_logged_at: float | None = None
        self._lock = asyncio.Lock()
        self._persist_lock = asyncio.Lock()
        self._revision = 0
        self._success_generation = 0

    async def start(self) -> None:
        data = await self._storage.panel_watch_load()
        if data is not None:
            self._last_ok = data["last_ok_at"]
            self._threshold = data["threshold"]
            self._chats = data["chats"]
            self._down = {int(chat): at for chat, at in data.get("down_notified_at", {}).items()}
            self._recovering = set(data.get("recovering", []))
            self._last_send = {int(chat): at for chat, at in data.get("last_send", {}).items()}
        await self.persist()

    def configure(self, config: NotifyConfig) -> None:
        self._chats = list(dict.fromkeys(config.chats))
        self._threshold = config.panel_down_after_seconds
        allowed = set(self._chats)
        self._down = {chat: at for chat, at in self._down.items() if chat in allowed}
        self._recovering.intersection_update(allowed)
        self._last_send = {chat: at for chat, at in self._last_send.items() if chat in allowed}
        self._revision += 1

    def on_ok(self) -> None:
        self._last_ok = self._clock()
        self._success_generation += 1
        self._recovering.update(self._down)
        self._revision += 1

    def on_alive(self) -> None:
        """401/429 suppress outage alarms but cannot confirm recovery of an existing incident."""
        self._last_ok = self._clock()
        self._revision += 1

    def on_fail(self) -> None:
        # Silence is measured from the last positive proof, including when no request is attempted.
        pass

    async def persist(self) -> None:
        async with self._persist_lock:
            # Callbacks are synchronous. Re-snapshot if one ran while SQLite committed in its worker thread.
            while True:
                revision = self._revision
                data: dict[str, Any] = {
                    "last_ok_at": self._last_ok,
                    "threshold": self._threshold,
                    "chats": list(self._chats),
                    "down_notified_at": dict(self._down),
                    "recovering": list(self._recovering),
                    "last_send": dict(self._last_send),
                }
                # to_thread cannot stop an SQLite worker when its awaiting coroutine is cancelled.
                # Keep ownership until that write finishes, so a final shutdown snapshot cannot be overtaken.
                pending = asyncio.create_task(self._storage.panel_watch_save(data))
                cancelled = False
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        cancelled = True
                pending.result()
                if cancelled:
                    raise asyncio.CancelledError
                if revision == self._revision:
                    return

    def _still_needed(self, chat: int, down_generation: int | None) -> bool:
        if chat not in self._chats:
            return False
        if down_generation is None:
            return chat in self._recovering
        return (
            down_generation == self._success_generation
            and self._clock() - self._last_ok >= self._threshold
            and chat not in self._recovering
        )

    async def _send(self, chat: int, text: str, *, down_generation: int | None = None) -> bool:
        for _ in range(3):
            if not self._still_needed(chat, down_generation):
                return False
            wait = self._last_send.get(chat, -float("inf")) + 1 - self._clock()
            if wait > 0:
                await self._sleep(wait)
            try:
                if self._pacer is not None:
                    await self._pacer.acquire(chat)
                if not self._still_needed(chat, down_generation):
                    return False
                self._last_send[chat] = self._clock()
                await self._bot.send_message(chat, text, parse_mode=None, disable_web_page_preview=True)
            except TelegramRetryAfter as error:
                if self._pacer is not None:
                    self._pacer.pause(error.retry_after)
                # A token-wide flood response pauses this sender before any other recipient is attempted.
                await self._sleep(max(1, error.retry_after))
            except TelegramForbiddenError:
                self._chats = [value for value in self._chats if value != chat]
                self._down.pop(chat, None)
                self._recovering.discard(chat)
                await self.persist()
                return False
            except TelegramAPIError as error:
                logger.warning("panel watch delivery failed (%s)", type(error).__name__)
                return False
            else:
                return True
        return False

    async def tick(self) -> None:
        async with self._lock:
            await self.persist()
            for chat in list(self._recovering):
                if await self._send(chat, t("panel_up")):
                    self._recovering.discard(chat)
                    self._down.pop(chat, None)
                    await self.persist()
            now = self._clock()
            if now - self._last_ok < self._threshold:
                return
            if not self._chats:
                if self._empty_logged_at is None or now - self._empty_logged_at >= self._repeat:
                    logger.info("the panel is unavailable; no notification chats configured")
                    self._empty_logged_at = now
                return
            for chat in list(self._chats):
                if chat in self._recovering:
                    continue
                now = self._clock()
                if now - self._last_ok < self._threshold:
                    break  # A heartbeat/claim may have completed during Telegram I/O.
                previous = self._down.get(chat)
                if previous is not None and now - previous < self._repeat:
                    continue
                generation = self._success_generation
                if await self._send(
                    chat, t("panel_down", minutes=int((now - self._last_ok) // 60)), down_generation=generation
                ):
                    self._down[chat] = self._clock()
                    if self._success_generation != generation:
                        self._recovering.add(chat)
                    await self.persist()
                # Stay below the token-wide limit even for a large administrator list.
                await self._sleep(0.05)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as error:
                logger.error("panel watch cycle failed (%s)", type(error).__name__)
            await self._sleep(INTERVAL)
