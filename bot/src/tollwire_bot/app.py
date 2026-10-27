"""Сборка диспетчера: точка входа и для запуска бота, и для тестов (подставляются клиент панели, хранилище, время)."""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, Message, TelegramObject

from tollwire_bot import commands, dialog
from tollwire_bot.config import Settings
from tollwire_bot.panel import PanelClient
from tollwire_bot.storage import SqliteStorage
from tollwire_bot.texts import t
from tollwire_bot.watcher import Watcher

# Бот публичный, писать ему может кто угодно, а каждый /start и /status - запрос в панель с общим лимитом на ключ.
# Лимит на человека не даёт одному выбить ключ у всех (регистрации, опрос готовности, heartbeat).
USER_LIMIT = 60  # событий
USER_WINDOW = 60.0  # за столько секунд


class UserThrottle(BaseMiddleware):
    """Не больше `limit` событий от одного человека за `window` секунд; лишние молча пропускаются.

    Об ограничении человеку сообщается один раз за окно. Память: очередь времён на человека, давние убираются."""

    def __init__(self, clock: Callable[[], float], limit: int = USER_LIMIT, window: float = USER_WINDOW) -> None:
        self._clock = clock
        self._limit = limit
        self._window = window
        self._hits: dict[int, deque[float]] = {}
        self._told: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        now = self._clock()
        hits = self._hits.setdefault(user.id, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            if now - self._told.get(user.id, -self._window) >= self._window:
                self._told[user.id] = now
                if isinstance(event, Message):
                    await event.answer(t("rate_limited"))
            if isinstance(event, CallbackQuery):
                await event.answer()  # нажатие подтверждаем, иначе клиент Telegram «крутит» кнопку
            return None
        hits.append(now)
        if len(self._hits) > 10_000:  # давно молчавшие люди не копятся
            for key in [key for key, queue in self._hits.items() if not queue or now - queue[-1] >= self._window]:
                del self._hits[key]
                self._told.pop(key, None)
        return await handler(event, data)


def build_router(clock: Callable[[], float] = time.time) -> Router:
    """Новый роутер на каждый диспетчер (роутер можно подключить только к одному родителю)."""
    router = Router()
    throttle = UserThrottle(clock)
    router.message.outer_middleware(throttle)
    router.callback_query.outer_middleware(throttle)
    # Только личные чаты: в группах бот молчит.
    router.message.filter(F.chat.type == "private", F.from_user)
    router.callback_query.filter(F.message.chat.type == "private")
    commands.register_commands(router)  # команды раньше состояний: /status работает и посреди диалога
    dialog.register(router)
    commands.register_fallback(router)
    return router


def build_dispatcher(
    settings: Settings,
    panel: PanelClient,
    storage: SqliteStorage,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.time,
) -> Dispatcher:
    # events_isolation: апдейты одного человека обрабатываются по очереди (два «Да» подряд - один POST).
    dp = Dispatcher(storage=storage, events_isolation=SimpleEventIsolation())
    watcher = Watcher(panel, storage, sleep=sleep, clock=clock)
    dp["panel"] = panel
    dp["watcher"] = watcher
    dp["settings"] = settings
    dp.include_router(build_router(clock))
    dp.startup.register(watcher.on_startup)
    dp.shutdown.register(watcher.on_shutdown)
    return dp
