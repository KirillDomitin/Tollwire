"""Тексты бота (texts.py): каждый ключ есть, подстановки сходятся с вызовами, нет HTML и подсказки /config.

Проверка вызовов `t(...)` идёт по исходникам пакета (AST), а не по поведению: опечатка в ключе или лишняя подстановка
в редкой ветке (сбой панели, таймаут) иначе всплыла бы только у живого человека."""

import ast
import re
from pathlib import Path

import pytest

import tollwire_bot
from support import K, all_keys, placeholders
from tollwire_bot.texts import TEXTS, t

PACKAGE = Path(tollwire_bot.__file__).resolve().parent
SOURCES = sorted(path for path in PACKAGE.glob("*.py") if path.name != "texts.py")


def trees() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in SOURCES]


def literal_calls() -> list[tuple[str, str, set[str]]]:
    """Вызовы t("ключ", ..., имя=...) с ключом-строкой: (файл, ключ, имена подстановок)."""
    found: list[tuple[str, str, set[str]]] = []
    for path, tree in trees():
        for node in ast.walk(tree):
            is_t = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t"
            if is_t and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                values = {kw.arg for kw in node.keywords if kw.arg and kw.arg != "lang"}
                found.append((path.name, node.args[0].value, values))
    return found


def test_the_package_sources_are_there() -> None:
    assert SOURCES, "bot/src/tollwire_bot must hold the modules the plan lists"


def test_every_text_has_a_russian_version() -> None:
    assert TEXTS
    for key, versions in TEXTS.items():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", key), key
        assert isinstance(versions["ru"], str) and versions["ru"].strip(), key


def test_every_key_the_tests_rely_on_exists() -> None:
    missing = [key for key in all_keys() if key not in TEXTS]
    assert not missing, f"texts.py lacks keys used by bot/tests (rename K in support.py or add them): {missing}"


def test_t_returns_the_russian_text_by_default() -> None:
    key = K.ask_name
    assert t(key) == TEXTS[key]["ru"] == t(key, "ru") == t(key, lang="ru")


def test_t_fills_in_the_placeholders() -> None:
    text = t(K.confirm, name="Иван", email="ivan@example.org")

    assert "Иван" in text and "ivan@example.org" in text and "{" not in text


def test_unknown_key_is_an_error_not_an_empty_string() -> None:
    with pytest.raises(KeyError):
        t("no_such_text_key")


def test_a_missing_placeholder_value_is_an_error() -> None:
    with pytest.raises(KeyError):
        t(K.confirm, name="Иван")  # {email} не задан


def test_every_call_uses_an_existing_key_with_exactly_its_placeholders() -> None:
    calls = literal_calls()
    assert calls, "the bot must say things through t()"
    problems = []
    for file, key, given in calls:
        if key not in TEXTS:
            problems.append(f"{file}: unknown key {key!r}")
        elif given != placeholders(TEXTS[key]["ru"]):
            problems.append(
                f"{file}: t({key!r}) gets {sorted(given)}, the text wants {sorted(placeholders(TEXTS[key]['ru']))}"
            )
    assert not problems, "\n".join(problems)


def test_no_text_is_dead() -> None:
    """Каждый ключ упомянут в коде бота строкой (прямо в t(...) или в таблице «причина -> ключ»)."""
    mentioned = {
        node.value
        for _, tree in trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    dead = sorted(set(TEXTS) - mentioned)
    assert not dead, f"texts nobody uses: {dead}"


def test_no_text_contains_html() -> None:
    """parse_mode не задан (имя и почта человека попадают в текст): разметке в текстах делать нечего."""
    for key, versions in TEXTS.items():
        assert not re.search(r"</?[A-Za-z][^>]*>", versions["ru"]), key


def test_no_text_advertises_config_yet() -> None:
    """/config и ссылка на конфиг - этап 25.3: пока подсказки о них не показываем."""
    for key, versions in TEXTS.items():
        assert "/config" not in versions["ru"], key


def test_every_text_fits_a_telegram_message() -> None:
    for key, versions in TEXTS.items():
        assert len(versions["ru"]) < 3000, key


@pytest.mark.parametrize(
    ("key", "fragments"),
    [
        (K.no_code, ["закрыт"]),
        (K.bad_link, ["недействительна"]),
        (K.already_registered, ["уже зарегистрированы", "/status"]),
        (K.not_registered, ["не зарегистрированы"]),
        (K.preparing, ["Готовим доступ"]),
        (K.failed, ["не получилось", "разберёмся"]),
        (K.still_preparing, ["ещё готовится", "/status"]),
        (K.ready_stub, ["Доступ готов до {until}", "Конфиг пришлёт администратор", "/status"]),
    ],
)
def test_texts_say_what_the_plan_says(key: str, fragments: list[str]) -> None:
    text = TEXTS[key]["ru"]
    for fragment in fragments:
        assert fragment.lower() in text.lower(), (key, fragment)


def test_every_invite_problem_has_its_own_text() -> None:
    keys = [K.bad_link, K.invite_used, K.invite_expired, K.invite_revoked, K.invite_unknown]

    assert len({t(key) for key in keys}) == len(keys)
