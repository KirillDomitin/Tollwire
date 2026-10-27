"""Логи бота. Секреты (токен Telegram, ключ панели) вырезаются из итоговой строки любого логгера, в том числе из
traceback: страховка на случай ошибки, сам бот токен и ключ не пишет. Данные людей (тексты сообщений, имя, почта, код
приглашения, тела запросов панели) бот в лог не отдаёт по построению."""

import logging
import re
import sys
from typing import TextIO

from tollwire_bot.config import Settings

_TOKEN_SHAPE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")  # токен BotFather
_KEY_SHAPE = re.compile(r"twb_[A-Za-z0-9_-]+")  # ключ бота в панели
_MASK = "***"


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        # Точные значения из настроек (самые длинные первыми: короткий не должен обрезать длинный).
        self._secrets = sorted((secret for secret in secrets if secret), key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)  # вместе с traceback
        for secret in self._secrets:
            text = text.replace(secret, _MASK)
        return _KEY_SHAPE.sub(_MASK, _TOKEN_SHAPE.sub(_MASK, text))


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Один обработчик на корневом логгере: ловит и свои записи, и aiogram, и httpx."""
    secrets = [settings.telegram_bot_token.get_secret_value(), settings.bot_api_key.get_secret_value()]
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(RedactingFormatter(secrets))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    level = logging.getLevelName(settings.log_level.upper())
    root.setLevel(level if isinstance(level, int) else logging.INFO)  # непонятный LOG_LEVEL не должен ронять бота
    # На INFO httpx пишет «HTTP Request: GET https://…/by-telegram/<id>»: адреса и telegram_id в журнале не нужны.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
