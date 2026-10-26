"""Heartbeat: getMe один раз при старте, затем POST /heartbeat каждые 30 с. Сбои пишутся в лог только при смене
состояния (панель упала / вернулась), чтобы остановленная панель не заливала журнал строкой раз в полминуты."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from aiogram import Bot

from tollwire_bot.panel import PanelClient

logger = logging.getLogger(__name__)

INTERVAL = 30


class Heartbeat:
    def __init__(
        self,
        bot: Bot,
        panel: PanelClient,
        *,
        version: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._panel = panel
        self._version = version
        self._sleep = sleep
        self._username = ""
        self._up: bool | None = None  # None - ещё не знаем
        self.last_ok_at: float | None = None  # monotonic: для healthcheck контейнера

    async def start(self) -> None:
        """Имя бота узнаём у Telegram один раз. Неверный токен - исключение: бот не должен молча «жить»."""
        me = await self._bot.get_me()
        self._username = me.username or ""
        logger.debug("the bot is @%s", self._username)

    async def beat(self) -> None:
        try:
            await self._panel.heartbeat(self._username, self._version)
        except Exception as exc:  # noqa: BLE001 - любой сбой: бот живёт дальше, панель может вернуться
            if self._up is not False:
                logger.warning("the panel does not answer the heartbeat (%s)", type(exc).__name__)
            self._up = False
            return
        if self._up is False:
            logger.info("the panel answers again")
        self._up = True
        self.last_ok_at = time.monotonic()

    async def run(self) -> None:
        while True:
            await self.beat()
            await self._sleep(INTERVAL)
