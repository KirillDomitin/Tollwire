"""Сборка диспетчера: точка входа и для запуска бота, и для тестов (подставляются клиент панели, хранилище, время)."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from aiogram import Dispatcher, F, Router
from aiogram.fsm.storage.memory import SimpleEventIsolation

from tollwire_bot import commands, dialog
from tollwire_bot.config import Settings
from tollwire_bot.panel import PanelClient
from tollwire_bot.storage import SqliteStorage
from tollwire_bot.watcher import Watcher


def build_router() -> Router:
    """Новый роутер на каждый диспетчер (роутер можно подключить только к одному родителю)."""
    router = Router()
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
    dp.include_router(build_router())
    dp.startup.register(watcher.on_startup)
    dp.shutdown.register(watcher.on_shutdown)
    return dp
