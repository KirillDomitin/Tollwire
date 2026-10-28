"""Тексты бота (texts.py): каждый ключ есть, подстановки сходятся с вызовами, нет HTML, /config подсказан там, где надо.

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


def test_the_stub_for_a_config_sent_by_the_admin_is_gone() -> None:
    """С этапа 25.3 конфиг не «присылает администратор»: человек получает ссылку на страницу сам."""
    assert "ready_stub" not in TEXTS
    for key, versions in TEXTS.items():
        assert "пришлёт администратор" not in versions["ru"], key


def test_config_is_advertised_where_a_person_looks_for_it() -> None:
    for key in (K.help, K.already_registered, K.status, K.ready_link):
        assert "/config" in TEXTS[key]["ru"], key


def test_the_link_message_warns_not_to_forward_and_shows_the_end_date() -> None:
    text = TEXTS[K.ready_link]["ru"]
    assert placeholders(text) == {"url", "until"}
    assert "пересыл" in text.lower()  # «не пересылайте»: ссылка открывает конфиг любому, у кого она есть


def test_the_link_message_puts_the_url_into_the_text_as_is() -> None:
    url = "https://panel.example.test/c#LinkToken0000000000000000000000000000000001"
    assert url in t(K.ready_link, url=url, until="25.11.2026")
    assert "25.11.2026" in t(K.ready_link, url=url, until="25.11.2026")


def test_the_menu_text_for_config_exists() -> None:
    assert TEXTS[K.cmd_config]["ru"].strip()


def test_every_text_fits_a_telegram_message() -> None:
    for key, versions in TEXTS.items():
        assert len(versions["ru"]) < 3000, key


@pytest.mark.parametrize(
    ("key", "fragments"),
    [
        (K.no_code, ["закрыт"]),
        (K.bad_link, ["недействительна"]),
        (K.already_registered, ["уже зарегистрированы", "/status", "/config"]),
        (K.not_registered, ["не зарегистрированы"]),
        (K.preparing, ["Готовим доступ"]),
        (K.failed, ["не получилось", "разберёмся"]),
        (K.still_preparing, ["ещё готовится", "/status"]),
        (K.ready_link, ["Доступ готов", "{url}", "{until}", "/config"]),
    ],
)
def test_texts_say_what_the_plan_says(key: str, fragments: list[str]) -> None:
    text = TEXTS[key]["ru"]
    for fragment in fragments:
        assert fragment.lower() in text.lower(), (key, fragment)


def test_every_config_refusal_has_its_own_text() -> None:
    keys = [K.config_unavailable, K.config_no_access, K.config_not_ready, K.config_not_configured]

    assert len({t(key) for key in keys}) == len(keys)


def test_every_invite_problem_has_its_own_text() -> None:
    keys = [K.bad_link, K.invite_used, K.invite_expired, K.invite_revoked, K.invite_unknown]

    assert len({t(key) for key in keys}) == len(keys)


# --- сообщение со ссылкой по рассылке админа (этап 25.4) -------------------------------------------------------------


def test_the_broadcast_link_message_takes_the_url_and_the_end_date() -> None:
    assert placeholders(TEXTS[K.broadcast_config]["ru"]) == {"url", "until"}


def test_the_broadcast_link_message_puts_the_url_and_the_date_in_as_they_are() -> None:
    url = "https://panel.example.test/c#LinkToken0000000000000000000000000000000001"

    text = t(K.broadcast_config, url=url, until="25.11.2026")

    assert url in text and "25.11.2026" in text and "{" not in text


def test_the_broadcast_link_message_says_the_old_link_no_longer_works_and_warns_not_to_forward() -> None:
    """Выдача по рассылке гасит прежнюю ссылку человека: он должен понять, почему старая перестала открываться."""
    text = TEXTS[K.broadcast_config]["ru"].lower()

    assert "прежн" in text and any(word in text for word in ("не работ", "не открыв", "перестан"))
    assert "пересыл" in text


def test_the_broadcast_link_message_is_plain_text_because_it_is_sent_without_parse_mode() -> None:
    assert not re.search(r"</?[A-Za-z][^>]*>", TEXTS[K.broadcast_config]["ru"])
    assert "&" not in TEXTS[K.broadcast_config]["ru"]


def test_the_broadcast_link_message_is_its_own_text_not_a_copy_of_the_answer_to_config() -> None:
    assert TEXTS[K.broadcast_config]["ru"] != TEXTS["config_link"]["ru"] != TEXTS[K.ready_link]["ru"]
