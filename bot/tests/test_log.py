"""Логи бота: токен Telegram, ключ панели и данные человека не попадают в вывод.

Фильтр стоит на корневом обработчике (ловит и чужие логгеры: aiogram, httpx): точные значения из настроек + шаблоны
токена BotFather и ключа `twb_…`. Сверх того бот сам не пишет тексты сообщений, данные FSM и тела запросов."""

import io
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from aiogram.methods import SendMessage

from fakes import BOT_TOKEN, conflict, rejected, unauthorized, unavailable
from support import (
    CODE,
    EMAIL,
    NAME,
    OTHER_EMAIL,
    SECRET_KEY,
    SECRET_TOKEN,
    K,
    make_settings,
    running_world,
    setup_logging,
    t,
)

ANOTHER_TOKEN = "123456789:" + "AAGq" * 9  # похож на токен BotFather, но в настройках его нет
ANOTHER_KEY = "twb_" + "Qw3" * 14 + "Qw"


@pytest.fixture(autouse=True)
def _restore_handlers() -> Iterator[None]:
    """`configure()` ниже убирает обработчики pytest; после теста они возвращаются на место."""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        yield
    finally:
        for handler in before:
            if handler not in root.handlers:
                root.addHandler(handler)


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


def configure(stream: io.StringIO, tmp_path: Path, **options: str) -> None:
    """Настроить логи бота так, как они настроены в работающем боте: один свой обработчик на корневом логгере.

    Обработчики pytest (caplog, отчёт) форматируют запись раньше и кладут сырой traceback в её кэш (`exc_text`), так что
    рядом с ними фильтр, не умеющий чистить исключения, выглядел бы исправным."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    setup_logging(make_settings(tmp_path, **options), stream)


# --- фильтр ----------------------------------------------------------------------------------------------------------


def test_an_ordinary_message_passes_unchanged(stream: io.StringIO, tmp_path: Path) -> None:
    configure(stream, tmp_path)

    logging.getLogger("tollwire_bot.test").warning("panel is back after %s seconds", 12)

    assert "panel is back after 12 seconds" in stream.getvalue()


@pytest.mark.parametrize(
    "secret",
    [BOT_TOKEN, SECRET_TOKEN, SECRET_KEY, ANOTHER_TOKEN, ANOTHER_KEY],
    ids=["exact short token", "exact token", "exact key", "token-shaped", "key-shaped"],
)
def test_secrets_are_removed_from_the_message(stream: io.StringIO, tmp_path: Path, secret: str) -> None:
    configure(stream, tmp_path, token=SECRET_TOKEN if secret != BOT_TOKEN else BOT_TOKEN, key=SECRET_KEY)
    log = logging.getLogger("tollwire_bot.test")

    log.error(f"connecting with {secret} now")
    log.error("connecting with %s now", secret)
    log.error("mapping %r", {"token": secret})

    output = stream.getvalue()
    assert output.count("connecting with") == 2 and "mapping" in output  # строки на месте, секрета в них нет
    assert secret not in output


def test_secret_in_a_telegram_url_is_removed(stream: io.StringIO, tmp_path: Path) -> None:
    configure(stream, tmp_path)

    logging.getLogger("aiohttp.client").error("POST https://api.telegram.org/bot%s/sendMessage failed", ANOTHER_TOKEN)

    assert "api.telegram.org" in stream.getvalue() and ANOTHER_TOKEN not in stream.getvalue()
    assert re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", stream.getvalue()) is None


def test_secret_in_a_traceback_is_removed(stream: io.StringIO, tmp_path: Path) -> None:
    configure(stream, tmp_path, token=SECRET_TOKEN, key=SECRET_KEY)

    try:
        raise RuntimeError(f"request to /bot{SECRET_TOKEN}/getMe with key {SECRET_KEY} failed")
    except RuntimeError:
        logging.getLogger("tollwire_bot.test").exception("handler failed")

    output = stream.getvalue()
    assert "handler failed" in output and "RuntimeError" in output
    assert SECRET_TOKEN not in output and SECRET_KEY not in output


def test_foreign_loggers_are_filtered_too(stream: io.StringIO, tmp_path: Path) -> None:
    configure(stream, tmp_path, token=SECRET_TOKEN)

    for name in ("aiogram.event", "aiogram.dispatcher", "httpx", "asyncio"):
        logging.getLogger(name).error("something with %s inside", SECRET_TOKEN)

    assert stream.getvalue().count("something with") == 4 and SECRET_TOKEN not in stream.getvalue()


def test_httpx_request_lines_are_not_written(stream: io.StringIO, tmp_path: Path) -> None:
    """httpx на INFO пишет «HTTP Request: POST https://…»: адреса панели и пути с telegram_id в журнале не нужны."""
    configure(stream, tmp_path, level="DEBUG")

    logging.getLogger("httpx").info('HTTP Request: GET https://panel.example.test/api/v1/bot/users/by-telegram/1 "200"')
    logging.getLogger("httpx").warning("httpx warns about something")
    logging.getLogger("tollwire_bot.test").debug("our own debug line")

    output = stream.getvalue()
    assert "HTTP Request" not in output
    assert "httpx warns about something" in output and "our own debug line" in output


@pytest.mark.parametrize(("level", "shown"), [("INFO", False), ("DEBUG", True)])
def test_log_level_comes_from_the_settings(stream: io.StringIO, tmp_path: Path, level: str, shown: bool) -> None:
    configure(stream, tmp_path, level=level)
    log = logging.getLogger("tollwire_bot.test")

    log.info("info line")
    log.debug("debug line")

    assert "info line" in stream.getvalue()
    assert ("debug line" in stream.getvalue()) is shown


def test_settings_do_not_show_secrets_when_printed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, token=SECRET_TOKEN, key=SECRET_KEY)

    shown = f"{settings!r} {settings!s} {settings.model_dump()}"

    assert SECRET_TOKEN not in shown and SECRET_KEY not in shown


# --- ссылка на страницу конфига (этап 25.3) --------------------------------------------------------------------------

LINK_TOKEN = "LinkToken" + "Zx7" * 11 + "Q"  # 43 символа, как у настоящего токена ссылки
LINK_URL = f"https://panel.example.test/c#{LINK_TOKEN}"


@pytest.mark.parametrize(
    "line",
    [
        LINK_URL,
        f"sending {LINK_URL} to the person",
        f"<{LINK_URL}>",
        f"link=({LINK_URL})",
        f"https://other.panel.example.org:8443/c#{LINK_TOKEN}",
    ],
    ids=["bare", "in a sentence", "in angle brackets", "in parentheses", "other host and port"],
)
def test_a_config_page_url_never_reaches_the_log(stream: io.StringIO, tmp_path: Path, line: str) -> None:
    """Токен в адресе открывает конфиг любому, у кого он есть: в журнале (его читают админы и собирают в сторонние
    системы) ссылки быть не должно - ни в тексте, ни в аргументах, ни в traceback, ни у чужих логгеров."""
    configure(stream, tmp_path)
    log = logging.getLogger("tollwire_bot.test")

    log.error(line)
    log.error("sending %s", line)
    log.error("mapping %r", {"url": line})
    logging.getLogger("aiogram.event").error("update failed: %s", line)
    try:
        raise RuntimeError(f"cannot send {line}")
    except RuntimeError:
        log.exception("handler failed")

    output = stream.getvalue()
    assert output.count("handler failed") == 1 and "RuntimeError" in output  # строки на месте
    assert LINK_TOKEN not in output


def test_the_log_keeps_ordinary_urls_of_the_panel_readable(stream: io.StringIO, tmp_path: Path) -> None:
    configure(stream, tmp_path)

    logging.getLogger("tollwire_bot.test").warning("the panel https://panel.example.test does not answer")

    assert "https://panel.example.test does not answer" in stream.getvalue()


def test_a_debug_line_with_the_text_of_a_sent_message_loses_the_link(stream: io.StringIO, tmp_path: Path) -> None:
    configure(stream, tmp_path, level="DEBUG")

    logging.getLogger("aiogram.event").debug("Update id=1 is handled. Duration 3 ms by bot id=42 text=%r", LINK_URL)

    assert LINK_TOKEN not in stream.getvalue()


# --- сквозной диалог -------------------------------------------------------------------------------------------------


async def test_a_whole_conversation_leaves_no_secrets_and_no_personal_data_in_the_log(
    stream: io.StringIO, tmp_path: Path
) -> None:
    """Всё подряд на уровне DEBUG: регистрация с ошибками ввода, занятая почта, сбои и 401 панели, /status, сломанная
    отправка. В выводе нет ни токена, ни ключа, ни имени, ни почты, ни кода приглашения, ни присланных текстов."""
    configure(stream, tmp_path, token=SECRET_TOKEN, key=SECRET_KEY, level="DEBUG")
    private_phrase = "мой личный секретный текст"
    async with running_world(tmp_path, token=SECRET_TOKEN, key=SECRET_KEY, level="DEBUG") as world:
        panel, ivan = world.panel, world.person()
        panel.add_invite(CODE)

        await ivan.say("/start")  # без кода
        await ivan.say(private_phrase)
        await ivan.start("bad code!")
        await ivan.start(CODE)
        await ivan.say("x" * 70)  # слишком длинное имя
        await ivan.say(NAME)
        await ivan.say("not-an-email")
        await ivan.say(OTHER_EMAIL)
        panel.override("POST /registrations", conflict("email_taken"))
        await ivan.press(K.btn_yes)  # почта занята
        await ivan.say(EMAIL)
        panel.override("POST /registrations", unavailable(), times=10)
        await ivan.press(K.btn_yes)  # панель лежит
        panel.recover()
        panel.override("POST /registrations", rejected("email", echo=EMAIL))
        await ivan.press(K.btn_yes)  # 422 со значением внутри
        await ivan.say(EMAIL)
        await ivan.press(K.btn_yes)
        panel.finish(panel.only_registration(), "active")
        await ivan.say("/status")
        await ivan.say("/config")  # человек просит ссылку: токен есть в ответе панели и в чате, но не в логе
        await ivan.say("/config")
        panel.override("GET /users/by-telegram/*", unauthorized())
        await ivan.say("/status")  # ключ отозван
        world.telegram.fail(SendMessage, RuntimeError(f"cannot reach /bot{SECRET_TOKEN}/sendMessage"))
        with pytest.raises(RuntimeError):
            await ivan.say("/help")

    output = stream.getvalue()
    assert output.strip(), "the conversation must leave at least some trace: otherwise the check below proves nothing"
    forbidden = [
        SECRET_TOKEN,
        SECRET_KEY,
        EMAIL,
        OTHER_EMAIL,
        NAME,
        CODE,
        private_phrase,
        panel.links[0].token,
        panel.links[-1].token,
        "Value error",
        t(K.preparing),
    ]
    leaked = [secret for secret in forbidden if secret in output]
    assert not leaked, f"in the log: {len(leaked)} of {len(forbidden)} forbidden values (first: {leaked[0][:8]}...)"
