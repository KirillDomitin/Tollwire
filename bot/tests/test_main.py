"""Запуск бота (`__main__.run`): настройки Bot и меню команд (этап 25.3).

Telegram и панель - заглушки: `Bot` подменён записывающей заменой, диспетчер не опрашивает сеть. Проверяется, с чем бот
создаётся и что кладёт в меню, а не работа aiogram."""

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
