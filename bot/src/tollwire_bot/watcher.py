"""Опрос готовности: после «Готовим доступ…» бот спрашивает панель о регистрации и сообщает человеку итог.

Расписание от момента регистрации: через 2, 5, 10 с, затем каждые 10 с, всего до 3 минут. Регистрации, за которыми
следим, лежат в хранилище: после рестарта бота опрос продолжается, отсчёт идёт от исходного момента, не от рестарта."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from tollwire_bot.panel import Conflict, PanelClient, PanelError
from tollwire_bot.refusals import CONFIG_REFUSAL_TEXT
from tollwire_bot.storage import SqliteStorage, Watch
from tollwire_bot.texts import format_date, t

logger = logging.getLogger(__name__)

SCHEDULE = (2, 5, 10, *range(20, 181, 10))  # секунды от начала; последний опрос на 180-й секунде = 3 минуты
_EPSILON = 1e-6


class Watcher:
    def __init__(
        self,
        panel: PanelClient,
        storage: SqliteStorage,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._panel = panel
        self._storage = storage
        self._sleep = sleep
        self._clock = clock
        self._bot: Bot | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # --- жизнь -------------------------------------------------------------------------------------------------------

    async def on_startup(self, bot: Bot) -> None:
        """Стартовый хук диспетчера: подхватить регистрации, которые ждали, пока бот был остановлен."""
        self._bot = bot
        for watch in await self._storage.watches():
            self._spawn(watch)

    async def on_shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def add(self, bot: Bot, registration_id: str, chat_id: int, telegram_id: int) -> None:
        self._bot = bot
        started_at = self._clock()
        await self._storage.add_watch(registration_id, chat_id, telegram_id, started_at)
        self._spawn(Watch(registration_id, chat_id, telegram_id, started_at))

    def _spawn(self, watch: Watch) -> None:
        if watch.registration_id in self._tasks:
            return
        self._tasks[watch.registration_id] = asyncio.create_task(self._run(watch))

    # --- слежение ----------------------------------------------------------------------------------------------------

    async def _run(self, watch: Watch) -> None:
        try:
            await self._follow(watch)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Без данных человека: что именно пошло не так, видно по traceback, а не по тексту.
            logger.exception("watching a registration failed")
        finally:
            self._tasks.pop(watch.registration_id, None)

    async def _follow(self, watch: Watch) -> None:
        polled = False
        while True:
            elapsed = self._clock() - watch.started_at
            due = next((offset for offset in SCHEDULE if offset > elapsed + _EPSILON), None)
            if due is None:  # три минуты вышли (возможно, пока бот лежал)
                if not polled and await self._poll(watch):  # один последний опрос: вдруг уже готово
                    return
                await self._finish(watch, t("still_preparing"))
                return
            await self._sleep(watch.started_at + due - self._clock())
            polled = True
            if await self._poll(watch):
                return

    async def _poll(self, watch: Watch) -> bool:
        """Один опрос. True - слежение закончено (итог сказан, сказать некому или регистрации больше нет)."""
        try:
            registration = await self._panel.registration(watch.registration_id)
        except PanelError:
            return False  # панель недоступна между опросами: пробуем в следующий срок
        if registration is None:
            await self._storage.remove_watch(watch.registration_id)
            return True
        if registration.status == "failed":
            return await self._tell(watch, t("failed"))
        if registration.status != "active":
            return False
        try:
            link = await self._panel.config_link(watch.telegram_id)
        except Conflict as refusal:
            text_key = CONFIG_REFUSAL_TEXT.get(refusal.reason)
            if text_key is None or refusal.reason == "not_ready":
                return False  # клиента ещё выдают (или причина неизвестна): спросим на следующем опросе
            return await self._tell(watch, t(text_key))  # страницы не будет: ответ не изменится
        except PanelError:
            return False
        if link is None:
            return False
        return await self._tell(watch, t("ready_link", url=link.config_url, until=format_date(link.valid_until)))

    async def _tell(self, watch: Watch, text: str) -> bool:
        """Сказать человеку. True - слежение закончено; False - не вышло, попробуем в следующий срок."""
        assert self._bot is not None
        try:
            await self._bot.send_message(watch.chat_id, text)
        except (TelegramForbiddenError, TelegramBadRequest):
            logger.info("the person cannot be told: blocked the bot or the chat is gone")
        except Exception:
            logger.warning("telling the person failed, will retry")
            return False
        await self._storage.remove_watch(watch.registration_id)
        return True

    async def _finish(self, watch: Watch, text: str) -> None:
        """Последняя попытка (таймаут): после неё слежение снимается при любом исходе."""
        assert self._bot is not None
        with contextlib.suppress(Exception):
            await self._bot.send_message(watch.chat_id, text)
        await self._storage.remove_watch(watch.registration_id)
