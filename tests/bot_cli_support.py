"""Общее для тестов консольных скриптов бота (этап 25.2): `scripts.bot_key` и `scripts.bot_invite`.

Тесты написаны до кода. Принятый контракт скриптов (всё, что план не фиксирует, - здесь, а не в десятке тестов):

- по образцу `scripts.create_admin`: `Console` (поля `interactive` и `say`; для чтения ключа из stdin - необязательное
  поле `read_stdin`, а если его нет, скрипт читает `sys.stdin`), `CliError`, `async run(argv, console)`;
- `run` возвращает `None`/`0` при успехе; отказ - `CliError` (или ненулевой код): оба считаются «не получилось».
  Неверные аргументы - `SystemExit` (argparse);
- `--expires-in` принимает число с единицей: `7d` (дни) или `12h` (часы);
- без терминала секрет (ключ, ссылка с кодом) можно получить только с `--raw`: тогда печатается он один, одной строкой.

Тексты проверяются по-русски (PANEL_LANG=ru, как в test_create_admin_cli.py).
"""

import io
import re
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

KEY = re.compile(r"twb_[A-Za-z0-9_-]{43}")
CODE_IN_LINK = re.compile(r"[?&]start=([A-Za-z0-9_-]{24})")
BOT_NAME = "tollwire_cli_bot"


@dataclass
class Outcome:
    """Чем закончился запуск скрипта: код выхода, всё, что он напечатал, текст отказа."""

    code: int
    said: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def screen(self) -> str:
        return "\n".join(self.said)

    @property
    def everything(self) -> str:
        """Экран вместе с текстом отказа: туда секрет тоже попадать не должен."""
        return f"{self.screen}\n{self.error}"


def console(module: ModuleType, *, interactive: bool, said: list[str], stdin: str | None) -> Any:
    options: dict[str, Any] = {"interactive": interactive, "say": said.append}
    if stdin is not None:
        options["read_stdin"] = lambda: stdin
    try:
        return module.Console(**options)
    except TypeError:  # у скрипта нет поля read_stdin: он читает sys.stdin (подменён в run_cli)
        options.pop("read_stdin", None)
        return module.Console(**options)


async def run_cli(
    module: ModuleType,
    *argv: str,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool = True,
    stdin: str | None = None,
) -> Outcome:
    """Запуск `module.run(argv, console)`. stdin - то, что скрипту подали в стандартный ввод (ключ для revoke/seen)."""
    said: list[str] = []
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        result = await module.run(list(argv), console(module, interactive=interactive, said=said, stdin=stdin))
    except module.CliError as error:
        return Outcome(1, said, str(error))
    except SystemExit as exit_:
        return Outcome(exit_.code if isinstance(exit_.code, int) and exit_.code else 2, said)
    return Outcome(int(result or 0), said)


def keys_in(text: str) -> list[str]:
    return KEY.findall(text)
