"""Запуск бота: python -m tollwire_bot. Настройки - из окружения (см. config.py)."""

import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand
from pydantic import ValidationError

from tollwire_bot import __version__
from tollwire_bot.app import build_dispatcher
from tollwire_bot.config import Settings
from tollwire_bot.heartbeat import Heartbeat
from tollwire_bot.log import configure_logging
from tollwire_bot.outbox import OutboxSender
from tollwire_bot.panel import PanelClient
from tollwire_bot.storage import SqliteStorage
from tollwire_bot.texts import t

logger = logging.getLogger("tollwire_bot")

ALIVE_FILE = Path("/tmp/alive")  # healthcheck контейнера: файл свежий, пока панель отвечает на heartbeat
ALIVE_MAX_AGE = 90  # секунд


async def keep_alive_file(heartbeat: Heartbeat) -> None:
    while True:
        if heartbeat.last_ok_at is not None and time.monotonic() - heartbeat.last_ok_at < ALIVE_MAX_AGE:
            with contextlib.suppress(OSError):
                ALIVE_FILE.touch()
        await asyncio.sleep(10)


PURGE_INTERVAL = 3600  # раз в час просроченные записи уходят с диска, даже если в бота никто не пишет


async def purge_loop(storage: SqliteStorage) -> None:
    while True:
        await asyncio.sleep(PURGE_INTERVAL)
        await storage.purge()


async def run(settings: Settings) -> int:
    panel = PanelClient(settings.panel_url, settings.bot_api_key.get_secret_value(), ca_file=settings.panel_ca_file)
    storage = SqliteStorage(settings.state_file)
    # Превью ссылок выключено: Telegram сам открыл бы страницу с ключом, чтобы нарисовать карточку.
    bot = Bot(
        settings.telegram_bot_token.get_secret_value(), default=DefaultBotProperties(link_preview_is_disabled=True)
    )
    dp = build_dispatcher(settings, panel, storage)
    heartbeat = Heartbeat(bot, panel, version=__version__)
    background: list[asyncio.Task[None]] = []
    try:
        try:
            await heartbeat.start()  # getMe: неверный токен виден сразу, а не молчанием бота
        except TelegramUnauthorizedError:
            logger.error("Telegram rejected the bot token: check TELEGRAM_BOT_TOKEN")
            return 1
        await bot.set_my_commands(
            [
                BotCommand(command="status", description=t("cmd_status")),
                BotCommand(command="config", description=t("cmd_config")),
                BotCommand(command="help", description=t("cmd_help")),
            ]
        )
        background = [
            asyncio.create_task(heartbeat.run()),
            asyncio.create_task(keep_alive_file(heartbeat)),
            asyncio.create_task(purge_loop(storage)),
            # Рассылки: тот же бот, что у диалога, - лимит Telegram один на токен.
            asyncio.create_task(
                OutboxSender(
                    bot,
                    panel,
                    storage,
                    rate_per_second=settings.outbox_rate_per_second,
                    batch=settings.outbox_batch,
                ).run()
            ),
        ]
        logger.info("the bot %s started", __version__)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())  # SIGTERM/SIGINT останавливают
        return 0
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await panel.aclose()
        await storage.close()
        await bot.session.close()


def main() -> None:
    try:
        settings = Settings()  # type: ignore[call-arg]  # поля читаются из окружения
    except ValidationError as error:
        # Только имена полей: в сообщении pydantic бывают введённые значения (токен, ключ).
        names = ", ".join(sorted({str(item["loc"][0]).upper() for item in error.errors() if item["loc"]}))
        sys.exit(f"invalid or missing settings: {names}")
    configure_logging(settings)
    try:
        code = asyncio.run(run(settings))
    except Exception:
        # Через логгер, а не через sys.excepthook: так трассировка проходит маскировку секретов.
        logger.exception("the bot stopped with an error")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
