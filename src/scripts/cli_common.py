#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Общее для консольных скриптов бота (`bot_key`, `bot_invite`): язык сообщений, консоль, понятные отказы.

Устройство то же, что у `create_admin`: `Console` — всё, что скрипт делает с терминалом, одним объектом, чтобы тесты
подставляли свой; `CliError` — причина отказа, которую печатают без трассировки."""

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field


class CliError(Exception):
    """Понятная человеку причина, по которой скрипт не может продолжить (печатается без трассировки)."""


@dataclass
class Console:
    interactive: bool  # и ввод, и вывод — терминал (секрет можно показать)
    say: Callable[[str], None] = field(default=print)
    read_stdin: Callable[[], str] = field(default=lambda: sys.stdin.read())  # ключ для revoke/seen: не из аргументов

    @classmethod
    def system(cls) -> "Console":
        return cls(interactive=sys.stdin.isatty() and sys.stdout.isatty())


def language() -> str:
    """PANEL_LANG (ru | en), иначе по локали системы, иначе английский (как у `create_admin`)."""
    for name in ("PANEL_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value:
            return "ru" if value.lower().startswith("ru") else "en"
    return "en"


def translator(messages: dict[str, dict[str, str]]) -> Callable[..., str]:
    def t(message: str, /, **values: object) -> str:  # message позиционный: подстановка может называться key
        return messages[message][language()].format(**values)

    return t
