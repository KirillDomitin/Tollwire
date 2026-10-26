"""Настоящий `PanelClient` бота (`bot/src/tollwire_bot/panel.py`) в тестах панели (этап 25.2).

Клиент написан так, что зависит только от httpx и stdlib (проверяет `unit/test_bot_isolation.py`), поэтому его можно
подключить из корневого окружения, где нет aiogram: добавляется только `bot/src` в путь импорта. Пакет `tollwire_bot`
в корневом `src/` не встречается, имён не пересекает.

Принятая сигнатура: `PanelClient(base_url, api_key, *, ca_file=None, transport=None, sleep=asyncio.sleep)`; `http://`
разрешён для внутренних имён без точки (`testserver` в тестах)."""

import sys
from pathlib import Path

BOT_SRC = Path(__file__).resolve().parents[1] / "bot" / "src"
if str(BOT_SRC) not in sys.path:
    sys.path.insert(0, str(BOT_SRC))

from tollwire_bot.panel import (  # noqa: E402
    Conflict,
    PanelAuthError,
    PanelClient,
    PanelUnavailable,
    RateLimited,
    Rejected,
)

__all__ = ["Conflict", "PanelAuthError", "PanelClient", "PanelUnavailable", "RateLimited", "Rejected"]
