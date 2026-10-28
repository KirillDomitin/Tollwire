"""Запуск бота (`__main__.run`): настройки Bot и меню команд (этап 25.3).

Telegram и панель - заглушки: `Bot` подменён записывающей заменой, диспетчер не опрашивает сеть. Проверяется, с чем бот
создаётся и что кладёт в меню, а не работа aiogram."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fakes import FakeClock, FakePanel
from support import K, make_client, make_settings, t


class StubBot:
    created: list["StubBot"] = []

    def __init__(self, token: str, **options: Any) -> None:
        self.token = token
        self.options = options
        self.commands: list[Any] = []
        self.session = SimpleNamespace(close=self._close)
        StubBot.created.append(self)

    async def _close(self) -> None:
        return None

    async def get_me(self) -> Any:
        return SimpleNamespace(username="tollwire_test_bot")

    async def set_my_commands(self, commands: list[Any]) -> bool:
        self.commands = list(commands)
        return True


class StubDispatcher:
    def resolve_used_update_types(self) -> list[str]:
        return ["message", "callback_query"]

    async def start_polling(self, bot: Any, **options: Any) -> None:
        return None  # сразу «остановлен»: run() доходит до конца


@pytest.fixture
async def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StubBot:
    """Бот, запущенный `run()` до остановки polling."""
    import tollwire_bot.__main__ as entry

    StubBot.created = []
    panel = FakePanel(FakeClock())
    monkeypatch.setattr(entry, "Bot", StubBot)
    monkeypatch.setattr(entry, "build_dispatcher", lambda *args, **kwargs: StubDispatcher())
    monkeypatch.setattr(
        entry,
        "PanelClient",
        lambda url, key, **options: make_client(panel, FakeClock(), key),
    )
    assert await entry.run(make_settings(tmp_path)) == 0
    (bot,) = StubBot.created
    return bot


async def test_links_are_sent_without_a_preview(started: StubBot) -> None:
    """Предпросмотр страницы по ссылке сходил бы на неё с серверов Telegram: токен в адресе - секрет."""
    default = started.options.get("default")
    assert default is not None, "Bot() must be created with DefaultBotProperties"
    assert default.link_preview_is_disabled is True


async def test_the_menu_has_status_config_and_help(started: StubBot) -> None:
    assert sorted(command.command for command in started.commands) == ["config", "help", "status"]
    assert all(command.description for command in started.commands)


async def test_the_menu_describes_config_with_the_text_from_texts(started: StubBot) -> None:
    (config,) = [command for command in started.commands if command.command == "config"]
    assert config.description == t(K.cmd_config)


# --- отправитель очереди (этап 25.4) ---------------------------------------------------------------------------------


class StubSender:
    """Вместо настоящего отправителя: запоминает, как его создали, шёл ли `run()` и отменили ли его при остановке."""

    created: list["StubSender"] = []

    def __init__(self, *args: Any, **options: Any) -> None:
        self.args, self.options = args, options
        self.running = False
        self.cancelled = False
        StubSender.created.append(self)

    async def run(self) -> None:
        self.running = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class PollingDispatcher(StubDispatcher):
    async def start_polling(self, bot: Any, **options: Any) -> None:
        await asyncio.sleep(0.05)  # фоновые задачи успевают начать работу


async def run_the_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **options: Any) -> tuple[StubBot, StubSender]:
    import tollwire_bot.__main__ as entry

    StubBot.created, StubSender.created = [], []
    panel = FakePanel(FakeClock())
    monkeypatch.setattr(entry, "Bot", StubBot)
    monkeypatch.setattr(entry, "OutboxSender", StubSender)
    monkeypatch.setattr(entry, "build_dispatcher", lambda *args, **kwargs: PollingDispatcher())
    monkeypatch.setattr(entry, "PanelClient", lambda url, key, **kw: make_client(panel, FakeClock(), key))
    assert await entry.run(make_settings(tmp_path, **options)) == 0
    (bot,) = StubBot.created
    (sender,) = StubSender.created
    return bot, sender


async def test_the_bot_starts_one_outbox_sender_with_the_defaults_for_rate_and_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, sender = await run_the_bot(tmp_path, monkeypatch)

    assert sender.options["rate_per_second"] == 20 and sender.options["batch"] == 50
    assert bot in (*sender.args, *sender.options.values())  # тот же бот, что у диалога: лимит Telegram один на токен


async def test_the_sender_gets_the_rate_and_batch_from_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sender = await run_the_bot(tmp_path, monkeypatch, outbox_rate_per_second=10, outbox_batch=25)

    assert sender.options["rate_per_second"] == 10 and sender.options["batch"] == 25


async def test_the_sender_works_in_the_background_and_is_stopped_with_the_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sender = await run_the_bot(tmp_path, monkeypatch)

    assert sender.running and sender.cancelled  # отмена задачи - то, как отправитель узнаёт о SIGTERM
