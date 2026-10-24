"""Маскировка секретов бота в логах (этап 25.1): заголовок `x-bot-key` и поле `code` (код приглашения)."""

import json
import logging

import pytest

from core.log import JsonFormatter, TextFormatter, redact, setup_logging


@pytest.fixture(autouse=True)
def _logging() -> None:
    setup_logging("debug", "text")  # ставит фабрику записей, которая снимает контекст


def make_record(msg: str, **extra: object) -> logging.LogRecord:
    return logging.getLogger("test").makeRecord("test", logging.INFO, __file__, 1, msg, (), None, extra=extra)


@pytest.mark.parametrize(
    "leak",
    [
        "x-bot-key: SECRETKEYVALUE123",
        "X-Bot-Key=SECRETKEYVALUE123",
        "{'x-bot-key': 'SECRETKEYVALUE123'}",
        '{"x-bot-key": "SECRETKEYVALUE123"}',
        '{"code": "SECRETKEYVALUE123"}',
        "code=SECRETKEYVALUE123",
        "code: SECRETKEYVALUE123",
    ],
)
def test_bot_key_header_and_invite_code_are_redacted(leak: str) -> None:
    assert "SECRETKEYVALUE123" not in redact(leak)
    for formatter in (JsonFormatter(), TextFormatter()):
        assert "SECRETKEYVALUE123" not in formatter.format(make_record(f"request failed {leak}"))


def test_redaction_keeps_the_field_name_so_the_log_stays_readable() -> None:
    assert "x-bot-key" in redact("x-bot-key: SECRETKEYVALUE123").lower()
    assert "code" in redact('{"code": "SECRETKEYVALUE123"}')


def test_redacted_json_line_stays_valid_json() -> None:
    line = JsonFormatter().format(make_record('body {"code": "SECRETKEYVALUE123", "name": "Ivan"}'))
    entry = json.loads(line)
    assert "SECRETKEYVALUE123" not in entry["msg"] and "Ivan" in entry["msg"]


def test_text_without_secrets_is_left_alone() -> None:
    text = "registration finished for user 3f2c, 2 jobs queued"
    assert redact(text) == text
