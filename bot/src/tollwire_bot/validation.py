"""Проверка ввода из Telegram (недоверенного). Окончательная проверка - в панели; здесь только очевидное."""

import re
import unicodedata

NAME_MAX = 64
EMAIL_MAX = 100  # vpn_users.email - VARCHAR(100)

_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # параметр start ссылки t.me: до 64 символов из этого алфавита
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Cc - управляющие, Cf - невидимые и направления (RLO, нулевая ширина), Cs - суррогаты, Zl/Zp - разделители строк и
# абзацев: по ним имя на экране админа можно подменить.
_FORBIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def valid_code(value: str) -> bool:
    return _CODE.fullmatch(value) is not None


def clean_name(value: str) -> str | None:
    """Имя без пробелов по краям или None, если оно не подходит."""
    name = value.strip()
    if not 1 <= len(name) <= NAME_MAX:
        return None
    if any(unicodedata.category(char) in _FORBIDDEN for char in name):
        return None
    return name


def clean_email(value: str) -> str | None:
    email = value.strip()
    if len(email) > EMAIL_MAX or _EMAIL.fullmatch(email) is None:
        return None
    return email
