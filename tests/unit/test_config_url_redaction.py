"""Маскировка адреса страницы конфига `…/c#<токен>` в логах панели (этап 25.4).

Ответ `POST /outbox/claim` несёт готовые адреса: при ошибке разбора или сбое прокси тело ответа может попасть в лог.
Токен лежит во фрагменте адреса, поэтому имя поля (`token=`), по которому маскирует 25.3, там не встречается."""

import json
import logging
import sys

import pytest

from core.log import JsonFormatter, TextFormatter, redact, setup_logging

TOKEN = "Qw3rTy_uIoP-aSdFgHjKlZxCvBnM0123456789abcde"  # 43 символа, как у настоящего токена ссылки
URL = f"https://panel.example.test/c#{TOKEN}"


@pytest.fixture(autouse=True)
def _logging() -> None:
    setup_logging("debug", "text")  # ставит фабрику записей, которая снимает контекст


def make_record(msg: str, **extra: object) -> logging.LogRecord:
    return logging.getLogger("test").makeRecord("test", logging.INFO, __file__, 1, msg, (), None, extra=extra)


@pytest.mark.parametrize(
    "leak",
    [
        URL,
        f"claim failed: {URL}",
        f"<{URL}>",
        f"({URL})",
        f'"{URL}"',
        f"{{'config_url': '{URL}', 'valid_until': '2026-12-01T00:00:00Z'}}",
        f'{{"config_url": "{URL}", "valid_until": "2026-12-01T00:00:00Z"}}',
        f'{{\\"config_url\\": \\"{URL}\\"}}',
        f"https://panel.example.test:8443/c#{TOKEN}",
        f"http://localhost/c#{TOKEN}",
        f"see {URL} and {URL}",
    ],
    ids=[
        "bare",
        "after text",
        "angle brackets",
        "parentheses",
        "quotes",
        "dict repr",
        "json",
        "escaped json",
        "with port",
        "plain http",
        "twice",
    ],
)
def test_the_token_of_a_page_address_is_redacted(leak: str) -> None:
    assert TOKEN not in redact(leak)
    for formatter in (JsonFormatter(), TextFormatter()):
        assert TOKEN not in formatter.format(make_record(f"outbox failed {leak}"))


def test_the_text_around_the_address_stays_readable() -> None:
    redacted = redact(f"claim failed for 3 items: {URL} (retry in 5 s)")

    assert TOKEN not in redacted
    assert redacted.startswith("claim failed for 3 items:") and redacted.endswith("(retry in 5 s)")


def test_a_redacted_json_line_stays_valid_json() -> None:
    line = JsonFormatter().format(make_record(f'body {{"config_url": "{URL}", "kind": "config"}}'))

    entry = json.loads(line)

    assert TOKEN not in entry["msg"] and '"kind": "config"' in entry["msg"]


def test_the_address_in_an_extra_field_is_redacted() -> None:
    record = make_record("claim answer", body=URL)

    assert TOKEN not in JsonFormatter().format(record) and TOKEN not in TextFormatter().format(record)


def test_the_address_in_a_traceback_is_redacted() -> None:
    try:
        raise RuntimeError(f"unexpected answer {URL}")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.getLogger("test").makeRecord("test", logging.ERROR, __file__, 1, "claim failed", (), exc_info)

    for formatter in (JsonFormatter(), TextFormatter()):
        output = formatter.format(record)
        assert "claim failed" in output and "RuntimeError" in output and TOKEN not in output


@pytest.mark.parametrize(
    "text",
    [
        "the page is opened at /c by the person",
        "https://panel.example.test/c",
        "https://panel.example.test/c/",
        "https://panel.example.test/api/v1/public/config",
        "C#sharp is a language",
        "anchor c#12",
    ],
)
def test_text_that_is_not_a_page_address_is_left_alone(text: str) -> None:
    assert redact(text) == text
