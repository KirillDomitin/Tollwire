#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Структурированные логи: одно событие — одна строка с полями (JSON в проде, key=value локально).

Контекст (request_id, job_id, server_id) хранится в contextvars и сам попадает в каждую запись,
сделанную внутри `bind(...)`: не нужно передавать его в каждый вызов logger.info.

Секреты в логи не пишем по построению (stdout SSH-команд, конфиги, токены не логируются). Маскировка
ниже — страховочная сетка на случай ошибки: она срабатывает на итоговой строке любого логгера.
"""

import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Literal

_context: ContextVar[Mapping[str, str]] = ContextVar("log_context", default={})


@contextmanager
def bind(**fields: object) -> Iterator[None]:
    """Добавляет поля ко всем записям внутри блока (и во вложенных вызовах, и в await). None — пропускается."""
    added = {key: str(value) for key, value in fields.items() if value is not None}
    token = _context.set({**_context.get(), **added})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> Mapping[str, str]:
    return _context.get()


# Атрибуты, которые есть у любой LogRecord; всё остальное в __dict__ — поля, переданные через extra=.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "context"}

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "***PRIVATE KEY***"),
    (re.compile(r"((?:Private|Preshared)Key\s*=\s*)[A-Za-z0-9+/=]+"), r"\1***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "***JWT***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (
        re.compile(
            r"(?i)(\\?\"?(?:password|secret|refresh_token|private_key|encryption_key|jwt_secret_key)\\?\"?\s*[:=]\s*\\?\"?)"
            r"[^\"\\\s,}&]+"
        ),
        r"\1***",
    ),
    # Ключ бота (заголовок X-Bot-Key) и код приглашения (поле code; сам код - 24 символа, короткие значения вроде
    # «exit code: 1» не трогаем). Имя поля может быть в кавычках любого вида: так оно выглядит и в JSON, и в repr dict.
    (
        re.compile(r"""(?i)((?<![\w-])(?:\\?["'])?x-bot-key(?:\\?["'])?\s*[:=]\s*\\?["']?)[^"'\\\s,}&]+"""),
        r"\1***",
    ),
    (
        re.compile(r"""(?i)((?<![\w-])(?:\\?["'])?code(?:\\?["'])?\s*[:=]\s*\\?["']?)[^"'\\\s,}&]{8,}"""),
        r"\1***",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _record_factory_with_context(base: Any) -> Any:
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record: logging.LogRecord = base(*args, **kwargs)
        record.context = dict(_context.get())  # снимок контекста в момент события
        return record

    factory._with_log_context = True  # type: ignore[attr-defined]
    return factory


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = dict(getattr(record, "context", {}))
    fields.update({key: value for key, value in vars(record).items() if key not in _STANDARD_ATTRS})
    return fields


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(entry, ensure_ascii=False, default=str))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        line = f"{ts} {record.levelname:<7} {record.name}: {record.getMessage()}"
        fields = " ".join(f"{key}={value}" for key, value in _fields(record).items())
        if fields:
            line = f"{line} | {fields}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return redact(line)


def setup_logging(level: str = "info", fmt: Literal["json", "text"] = "text") -> None:
    """Один обработчик на stdout у корневого логгера; логи uvicorn и библиотек идут через него же.

    Повторный вызов безопасен: заменяет только свой обработчик (чужие, например pytest, не трогает).
    """
    if not getattr(logging.getLogRecordFactory(), "_with_log_context", False):
        logging.setLogRecordFactory(_record_factory_with_context(logging.getLogRecordFactory()))

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_vpn_panel", False)]:
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler._vpn_panel = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn настраивает свои логгеры до импорта приложения — переводим их на общий формат.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Строку на каждый запрос пишет наш middleware (с request_id); access-лог uvicorn её бы дублировал.
    logging.getLogger("uvicorn.access").disabled = True
    # asyncssh на INFO пишет каждый канал и каждую команду — оставляем только предупреждения.
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
