"""Тексты бота 0.3.0: слежение за панелью и привязка админа. Общие проверки ключей и вызовов - `test_texts.py`
(в нём `test_every_key_the_tests_rely_on_exists` покажет, каких ключей не хватает)."""

import re

import pytest

import tollwire_bot
from support import K, placeholders
from tollwire_bot.texts import TEXTS, t

NEW_KEYS = [
    K.panel_down,
    K.panel_up,
    K.admin_linked,
    K.admin_link_usage,
    K.admin_link_invalid,
    K.admin_link_expired,
    K.admin_link_taken,
]


def test_the_bot_version_is_0_3_0() -> None:
    assert tollwire_bot.__version__ == "0.3.0"


@pytest.mark.parametrize("key", NEW_KEYS)
def test_every_new_text_exists_and_is_russian_plain_text(key: str) -> None:
    text = TEXTS[key]["ru"]

    assert text.strip() and re.search("[А-Яа-я]", text)
    assert not re.search(r"</?[a-z]+>", text)  # уведомления и ответы идут без parse_mode


def test_the_outage_text_carries_the_minutes() -> None:
    assert placeholders(TEXTS[K.panel_down]["ru"]) == {"minutes"}
    assert "7" in t(K.panel_down, minutes=7)


def test_the_recovery_text_has_no_placeholders() -> None:
    assert placeholders(TEXTS[K.panel_up]["ru"]) == set()


def test_the_link_confirmation_names_the_admin() -> None:
    assert placeholders(TEXTS[K.admin_linked]["ru"]) == {"name"}
    assert "Анна" in t(K.admin_linked, name="Анна")


@pytest.mark.parametrize("key", [K.admin_link_usage, K.admin_link_invalid, K.admin_link_expired, K.admin_link_taken])
def test_refusal_texts_have_no_placeholders_and_never_hold_a_code(key: str) -> None:
    assert placeholders(TEXTS[key]["ru"]) == set()


def test_the_refusals_are_different_texts() -> None:
    refusals = [t(K.admin_link_invalid), t(K.admin_link_expired), t(K.admin_link_taken), t(K.admin_link_usage)]

    assert len(set(refusals)) == 4
