"""Настоящий `PanelClient` бота (`bot/src/tollwire_bot/panel.py`) в тестах панели (этап 25.2).

Клиент написан так, что зависит только от httpx и stdlib (проверяет `unit/test_bot_isolation.py`), поэтому его можно
подключить из корневого окружения, где нет aiogram: добавляется только `bot/src` в путь импорта. Пакет `tollwire_bot`
в корневом `src/` не встречается, имён не пересекает.

Принятая сигнатура: `PanelClient(base_url, api_key, *, ca_file=None, transport=None, sleep=asyncio.sleep)`; `http://`
разрешён для внутренних имён без точки (`testserver` в тестах).

Этап 25.4: `PanelClient.claim(limit) -> Claim` (`lease_id`, `lease_until`, `items`, `texts`; у элемента `id`, `kind`,
`telegram_id`, `broadcast_id`, `config_url`, `valid_until`) и `PanelClient.ack(lease_id, results)` (результаты -
`OutboxResult(id, status, error=None, retry_after=None)` из `tollwire_bot.panel`, ответ - `applied` и `ignored`).
Они подключаются внутри функций: файл не падает на импорте, пока кода нет."""

import sys
from pathlib import Path
from typing import Any

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

__all__ = [
    "Conflict",
    "PanelAuthError",
    "PanelClient",
    "PanelUnavailable",
    "RateLimited",
    "Rejected",
    "outbox_result",
]


def outbox_result(item_id: Any, status: str, error: str | None = None, retry_after: int | None = None) -> Any:
    """Итог одной отправки для `PanelClient.ack`."""
    from tollwire_bot.panel import OutboxResult

    return OutboxResult(id=str(item_id), status=status, error=error, retry_after=retry_after)
