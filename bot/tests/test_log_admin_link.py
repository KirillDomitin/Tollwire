"""Маска кода привязки в логах бота (этап 25.5): `start=<код>` в ссылке и `code=<код>` вырезаются, имя поля остаётся."""

import logging

import pytest

ADMIN_CODE = "AdminCode_" + "x" * 22


def format_line(message: str) -> str:
    from tollwire_bot.log import RedactingFormatter

    record = logging.LogRecord("tollwire_bot.test", logging.INFO, __file__, 1, message, None, None)
    return RedactingFormatter(["secret-value"]).format(record)


@pytest.mark.parametrize(
    "line",
    [
        f"opened https://t.me/tollwire_bot?start={ADMIN_CODE}",
        f"url=t.me/tollwire_bot?start={ADMIN_CODE}&x=1",
        f"payload start={ADMIN_CODE}",
        f"body code={ADMIN_CODE}",
    ],
)
def test_the_admin_link_code_is_masked(line: str) -> None:
    assert ADMIN_CODE not in format_line(line)


def test_the_field_name_stays_so_the_line_is_readable() -> None:
    assert "start=" in format_line(f"https://t.me/tollwire_bot?start={ADMIN_CODE}")


def test_the_mask_covers_a_code_inside_a_traceback() -> None:
    try:
        raise RuntimeError(f"bad start={ADMIN_CODE}")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())

    from tollwire_bot.log import RedactingFormatter

    assert ADMIN_CODE not in RedactingFormatter(["secret-value"]).format(record)


def test_a_line_without_codes_is_left_alone() -> None:
    assert format_line("the panel answers again").endswith("the panel answers again")
