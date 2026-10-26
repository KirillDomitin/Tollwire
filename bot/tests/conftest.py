"""Фикстуры тестов бота: сцена `World` (бот над фейками) и чистка логирования.

Окружение отдельное (`uv run --project bot pytest`): корневой `src/` и его БД здесь недоступны, как и в самом боте."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from support import World, clean_logging, running_world


@pytest.fixture(autouse=True)
def _isolated_logging() -> Iterator[None]:
    with clean_logging():
        yield


@pytest.fixture(autouse=True)
def _no_ambient_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Токен и ключ разработчика из его окружения тестам не нужны: настройки собираются из аргументов."""
    for name in ("TELEGRAM_BOT_TOKEN", "BOT_API_KEY", "PANEL_URL", "PANEL_CA_FILE", "STATE_DIR", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    async with running_world(tmp_path) as running:
        yield running
