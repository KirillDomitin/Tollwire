"""Общее для тестов бота (этап 25.2): сцена `World`, собеседник `Person`, фабрики объектов бота.

Тесты написаны до кода. Всё, что план не фиксирует (сигнатуры конструкторов, имена методов, ключи текстов), собрано
ЗДЕСЬ: если реализация назвала что-то иначе, правится этот файл, а не десяток тестов. Принятые допущения:

- `PanelClient(base_url, api_key, *, ca_file=None, transport=None, sleep=asyncio.sleep)`; методы: `heartbeat(
  bot_username, version)`, `check_invite(code) -> str`, `register(code=, telegram_id=, telegram_username=, name=,
  email=)`, `registration(registration_id)` и `user_by_telegram(telegram_id)` (оба - `None` при 404), `aclose()`.
  Результаты - объекты с атрибутами по именам полей API (`registration_id`, `status`, `config_url`, `name`,
  `valid_until`, `protocol`). Ошибки `PanelAuthError`, `PanelUnavailable`, `Conflict.reason`, `Rejected.field`,
  `RateLimited` - в `tollwire_bot.panel`. Адрес, не прошедший проверку (http не во внутреннюю сеть), - `ValueError`.
- `build_dispatcher(settings, panel, storage, *, sleep=, clock=)`: `sleep` и `clock` (секунды с эпохи, как
  `time.time`) нужны, чтобы опрос длиной в 3 минуты шёл на виртуальном времени; `clock` же пишется в `started_at`
  ожидающей регистрации. Опрос после рестарта запускается стартовым хуком диспетчера (`dp.emit_startup`), остановка
  фоновых задач - `dp.emit_shutdown`.
- `SqliteStorage(path, *, ttl=86400, clock=time.time)` (path - файл БД) и список ожидающих регистраций:
  `add_watch(registration_id, chat_id, telegram_id, started_at)`, `watches()` (элементы с `registration_id`),
  `remove_watch(registration_id)`.
- `Heartbeat(bot, panel, *, version, sleep=)`: `start()` (getMe один раз), `beat()` (один тик: POST /heartbeat, лог при
  смене состояния ok/down), `run()` (цикл: тик, `sleep(30)`).
- `configure_logging(settings, *, stream)` ставит фильтр на корневой обработчик, пишущий в `stream`.
- `Settings(telegram_bot_token=, bot_api_key=, panel_url=, state_dir=, log_level=)` (pydantic-settings, SecretStr).
- Этап 25.3: `PanelClient.config_link(telegram_id)` -> объект с `config_url` и `valid_until`, `None` при 404 (как у
  `user_by_telegram`); 409 с `reason` (`config_unavailable`, `not_ready`, `no_access`, `not_configured`) - `Conflict`.
  Регистрация и статус больше не несут `config_url`: ссылку выдаёт только `config_link`. Текст `ready_link` берёт
  `{url}` и `{until}` (срок - `valid_until` из ответа `config_link`); `ready_stub` удалён. Команда `/config`, пункт
  меню `config`, ключи отказов `/config` - в `K` ниже (`config_*`). Текст самой новой ссылки в ответе на `/config` в
  тестах ищется по адресу (`Person.links()`), поэтому его ключ в `K` не нужен.
- Ключи текстов (`K`): если у реализации они называются иначе - правится только `K` ниже. Тест
  `test_texts.py::test_every_key_the_tests_rely_on_exists` покажет, каких ключей нет.

Модули бота подключаются внутри функций: файл тестов шага плана видит только модули своего шага (панель - шаг 2,
диалог - шаг 3), а не падает на импорте того, чего ещё нет.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey

from fakes import BOT_TOKEN, FakeClock, FakePanel, FakeTelegram, Updates

if TYPE_CHECKING:
    from tollwire_bot.config import Settings
    from tollwire_bot.heartbeat import Heartbeat
    from tollwire_bot.panel import PanelClient
    from tollwire_bot.storage import SqliteStorage

PANEL_URL = "https://panel.example.test"
TG_ID = 5550001234
OTHER_TG_ID = 5550004321
NAME = "Иван Петров"
EMAIL = "ivan.petrov@example.org"
OTHER_EMAIL = "maria.sidorova@example.org"
CODE = "Inv_Code-123"
OTHER_CODE = "Other_Code-456"
DEFAULT_KEY = "twb_" + "k" * 43

# Секреты для проверки логов: форма настоящих (токен BotFather и ключ панели), значения выдуманные.
SECRET_TOKEN = "7000000001:AAF3x9Qk1ZpLm2NvB8cD4eR6tY0uI5oP7aS"
SECRET_KEY = "twb_" + "Zq8" * 14 + "Zq"


class K:
    """Ключи текстов бота, на которые опираются тесты (`t(K.x)`)."""

    # /start
    no_code = "no_code"  # без кода: «сервис в закрытом тестировании»
    bad_link = "bad_link"  # код не по формату: «ссылка недействительна»
    already_registered = "already_registered"  # «вы уже зарегистрированы, /status»
    invite_used = "invite_used"
    invite_expired = "invite_expired"
    invite_revoked = "invite_revoked"
    invite_unknown = "invite_unknown"
    # шаги диалога
    ask_name = "ask_name"
    bad_name = "bad_name"
    ask_email = "ask_email"
    bad_email = "bad_email"
    confirm = "confirm"  # {name}, {email}
    btn_yes = "btn_yes"
    btn_edit = "btn_edit"
    email_taken = "email_taken"  # почта занята: введите другую
    # отправка и ожидание
    preparing = "preparing"  # «Готовим доступ…»
    ready_link = "ready_link"  # {url}, {until}: доступ готов, вот ссылка, срок и «не пересылайте; новая - /config»
    failed = "failed"  # «не получилось, мы разберёмся»
    still_preparing = "still_preparing"  # «ещё готовится, проверьте /status»
    # команды
    status = "status"  # {name}, {until}, {protocol}; подсказка про /config
    # отказы /config и watcher'а по причине 409 панели
    config_unavailable = "config_unavailable"  # клиент импортирован: страницы конфига нет
    config_no_access = "config_no_access"  # доступ закончился или отозван
    config_not_ready = "config_not_ready"  # клиент ещё выдаётся
    config_not_configured = "config_not_configured"  # у панели не задан публичный адрес
    cmd_config = "cmd_config"  # описание команды /config в меню бота
    not_registered = "not_registered"
    help = "help"
    unknown_input = "unknown_input"
    # сбои панели
    panel_unavailable = "panel_unavailable"
    rate_limited = "rate_limited"


def all_keys() -> list[str]:
    return [value for name, value in vars(K).items() if not name.startswith("_")]


def t(key: str, **values: Any) -> str:
    from tollwire_bot.texts import t as translate

    return translate(key, **values)


LINK_RE = re.compile(r"https?://[^\s]+/c#[A-Za-z0-9_-]+")


def ready_link_text(url: str, until: str | None = None) -> str:
    """Сообщение «доступ готов» со ссылкой; срок по умолчанию - тот, что отдаёт FakePanel."""
    return t(K.ready_link, url=url, until=FAKE_UNTIL if until is None else until)


def date_of(iso: str) -> str:
    """'2026-11-25T12:00:00Z' -> '25.11.2026': так бот показывает срок доступа."""
    year, month, day = iso[:10].split("-")
    return f"{day}.{month}.{year}"


FAKE_UNTIL = date_of("2026-11-25T12:00:00Z")


# --- фабрики ---------------------------------------------------------------------------------------------------------


def make_settings(tmp_path: Path, *, token: str = BOT_TOKEN, key: str = DEFAULT_KEY, level: str = "INFO") -> Settings:
    from tollwire_bot.config import Settings

    return Settings(  # type: ignore[call-arg]
        telegram_bot_token=token,
        bot_api_key=key,
        panel_url=PANEL_URL,
        state_dir=tmp_path,
        log_level=level,
        _env_file=None,
    )


def make_client(panel: FakePanel, clock: FakeClock, key: str | None = None) -> PanelClient:
    from tollwire_bot.panel import PanelClient

    return PanelClient(PANEL_URL, key or panel.key, transport=panel.transport, sleep=clock.sleep)


def make_storage(path: Path, clock: FakeClock | None = None, **options: Any) -> SqliteStorage:
    from tollwire_bot.storage import SqliteStorage

    if clock is not None:
        options.setdefault("clock", clock.wall)
    return SqliteStorage(path, **options)


async def add_watch(storage: SqliteStorage, registration_id: str, chat_id: int, started_at: float = 0.0) -> None:
    await storage.add_watch(registration_id, chat_id, chat_id, started_at)


async def watch_ids(storage: SqliteStorage) -> list[str]:
    return [str(watch.registration_id) for watch in await storage.watches()]


def make_heartbeat(world: World) -> Heartbeat:
    from tollwire_bot import __version__
    from tollwire_bot.heartbeat import Heartbeat

    return Heartbeat(world.bot, world.client, version=__version__, sleep=world.clock.sleep)


def setup_logging(settings: Settings, stream: Any) -> None:
    from tollwire_bot.log import configure_logging

    configure_logging(settings, stream=stream)


async def wait_until(condition: Callable[[], bool], *, timeout: float = 10.0, what: str = "the condition") -> None:
    """Ждёт настоящее время, пока бот и фоновые задачи дойдут до нужного (виртуальное время идёт само)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def settle(seconds: float = 0.1) -> None:
    """Пауза для проверок «больше ничего не происходит»: бот успевает отреагировать, если собирался."""
    await asyncio.sleep(seconds)


# --- собеседник ------------------------------------------------------------------------------------------------------


class Person:
    """Человек в Telegram: пишет боту и нажимает кнопки; видит то, что бот ему отправил."""

    def __init__(self, world: World, user_id: int, username: str | None, chat_type: str = "private") -> None:
        self.world = world
        self.id = user_id
        self.username = username
        self.chat_type = chat_type

    async def say(self, text: str) -> None:
        update = self.world.updates.message(self.id, text, username=self.username, chat_type=self.chat_type)
        await self.world.dp.feed_raw_update(self.world.bot, update)

    async def send_sticker(self) -> None:
        await self.world.dp.feed_raw_update(self.world.bot, self.world.updates.sticker(self.id, username=self.username))

    def storage_key(self) -> StorageKey:
        """Ключ диалога этого человека в FSM-хранилище бота."""
        return StorageKey(bot_id=self.world.bot.id, chat_id=self.id, user_id=self.id)

    async def press(self, key: str) -> None:
        """Нажимает кнопку с текстом `t(key)`. Нет такой видимой кнопки - тест падает (её нельзя было нажать)."""
        label = t(key)
        visible = self.world.telegram.live_buttons(self.id)
        buttons = [button for button in visible if button.text == label]
        assert buttons, f"no visible button {label!r}; visible: {[button.text for button in visible]}"
        await self.press_button(buttons[-1])

    async def press_button(self, button: Any) -> None:
        shown = self.world.telegram.messages[(self.id, button.message_id)].text
        update = self.world.updates.callback(self.id, button, shown, username=self.username)
        await self.world.dp.feed_raw_update(self.world.bot, update)

    # --- что человек видит
    @property
    def texts(self) -> list[str]:
        return self.world.telegram.texts(self.id)

    @property
    def last(self) -> str:
        assert self.texts, "the bot has said nothing yet"
        return self.texts[-1]

    def heard(self, key: str, **values: Any) -> bool:
        return t(key, **values) in self.texts

    def links(self) -> list[str]:
        """Адреса страниц конфига во всём, что бот сказал человеку, по порядку."""
        return [match for text in self.texts for match in LINK_RE.findall(text)]

    def buttons(self) -> list[str]:
        return [button.text for button in self.world.telegram.live_buttons(self.id)]

    # --- сценарии
    async def start(self, code: str = CODE) -> None:
        await self.say(f"/start {code}")

    async def to_confirm(self, code: str = CODE, name: str = NAME, email: str = EMAIL) -> None:
        """/start -> имя -> почта: бот показывает «верно?» с кнопками."""
        await self.start(code)
        await self.say(name)
        await self.say(email)
        assert self.last == t(K.confirm, name=name.strip(), email=email), self.texts

    async def register(self, code: str = CODE, name: str = NAME, email: str = EMAIL) -> None:
        await self.to_confirm(code, name, email)
        await self.press(K.btn_yes)


# --- сцена -----------------------------------------------------------------------------------------------------------


@dataclass
class World:
    """Бот целиком над фейками: диспетчер, хранилище в файле, клиент панели, Telegram без сети, виртуальное время."""

    tmp_path: Path
    clock: FakeClock
    panel: FakePanel
    telegram: FakeTelegram
    bot: Bot
    settings: Settings
    updates: Updates = field(default_factory=Updates)
    storage: SqliteStorage = field(init=False)
    client: PanelClient = field(init=False)
    dp: Dispatcher = field(init=False)
    driver: asyncio.Task[None] | None = None

    @property
    def state_file(self) -> Path:
        return self.tmp_path / "state.sqlite3"

    def person(self, user_id: int = TG_ID, username: str | None = "ivan_p", chat_type: str = "private") -> Person:
        return Person(self, user_id, username, chat_type)

    def polling_data(self) -> dict[str, Any]:
        """Что aiogram передаёт хукам при `start_polling` (кроме самого `bot`)."""
        return {"dispatcher": self.dp, "bots": [self.bot], **self.dp.workflow_data}

    async def boot(self) -> None:
        """Запуск процесса бота: хранилище, клиент панели, диспетчер, стартовые хуки (там же возобновляется опрос)."""
        from tollwire_bot.app import build_dispatcher

        self.storage = make_storage(self.state_file, self.clock)
        self.client = make_client(self.panel, self.clock)
        self.dp = build_dispatcher(
            self.settings, self.client, self.storage, sleep=self.clock.sleep, clock=self.clock.wall
        )
        await self.dp.emit_startup(bot=self.bot, **self.polling_data())

    async def stop(self) -> None:
        """Остановка процесса (SIGTERM): фоновые задачи гасятся, хранилище и клиент закрываются."""
        await self.dp.emit_shutdown(bot=self.bot, **self.polling_data())
        await self.client.aclose()
        await self.storage.close()

    async def restart(self) -> None:
        """Процесс бота перезапущен: Telegram, панель и время остались, память и задачи - нет, файл состояния - да."""
        await self.stop()
        await self.boot()


@contextlib.asynccontextmanager
async def running_world(
    tmp_path: Path, *, token: str = BOT_TOKEN, key: str = DEFAULT_KEY, level: str = "INFO"
) -> AsyncIterator[World]:
    clock = FakeClock()
    panel = FakePanel(clock, key=key)
    telegram = FakeTelegram()
    telegram.clock = clock
    bot = Bot(token)
    bot.session.middleware(telegram)
    world = World(tmp_path, clock, panel, telegram, bot, make_settings(tmp_path, token=token, key=key, level=level))
    world.driver = asyncio.create_task(clock.drive())
    await world.boot()
    try:
        yield world
    finally:
        await world.stop()
        world.driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await world.driver
        await bot.session.close()


# --- логи ------------------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def clean_logging() -> Iterator[None]:
    """Возвращает корневой логгер как был: настройка логов бота не должна утекать в другие тесты."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    saved = {name: logging.getLogger(name).level for name in ("httpx", "httpcore", "aiogram")}
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
        root.setLevel(level)
        for name, value in saved.items():
            logging.getLogger(name).setLevel(value)


def placeholders(text: str) -> set[str]:
    """Имена подстановок {name} в тексте (формат str.format)."""
    return set(re.findall(r"\{(\w+)\}", text))
