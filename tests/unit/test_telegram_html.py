"""Проверка Telegram HTML для новостей (этап 25.4, Р3-A): строгий белый список с отказом, текст молча не меняется.

Общий набор примеров `frontend/src/lib/telegramHtml.cases.json` читают и этот файл, и vitest: проверка на сервере и
предпросмотр в браузере не должны разойтись. Здесь же - примеры, которые нужны только серверу (позиция ошибки,
пределы исходника, обходы белого списка), в набор их класть не надо.

Принятый контракт - в блоке допущений `tests/bot_support.py`: `check_telegram_html(text) -> int` возвращает видимую
длину в единицах UTF-16, при отказе бросает `TelegramHtmlError` с `.code` и `.position`."""

import json
from pathlib import Path
from typing import Any

import pytest

CASES_FILE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "telegramHtml.cases.json"
CODES = {
    "telegram_html_tag",
    "telegram_html_attr",
    "telegram_html_entity",
    "telegram_html_nesting",
    "telegram_html_length",
    "telegram_html_empty",
}
MAX_VISIBLE = 4096
MAX_SOURCE = 16384
RLO, LRI, PDI, RLE, PDF, FSI, ZWJ = "\u202e", "\u2066", "\u2069", "\u202b", "\u202c", "\u2068", "\u200d"


def shared_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return cases


def run(text: str) -> tuple[int | None, Any]:
    """(видимая длина, None) при успехе или (None, ошибка) при отказе; любая другая ошибка - сбой теста."""
    from modules.bot.telegram_html import TelegramHtmlError, check_telegram_html

    try:
        return check_telegram_html(text), None
    except TelegramHtmlError as error:
        return None, error


def refusal(text: str) -> Any:
    length, error = run(text)
    assert error is not None, f"accepted, visible length {length}: {text[:80]!r}"
    return error


# --- общий набор примеров ------------------------------------------------------------------------------------------


def test_the_shared_file_is_well_formed_and_covers_every_error_code() -> None:
    cases = shared_cases()
    assert len(cases) >= 40
    names = [case["name"] for case in cases]
    assert len(names) == len(set(names)), "case names must be unique"
    for case in cases:
        assert isinstance(case["input"], str) and isinstance(case["ok"], bool), case["name"]
        if case["ok"]:
            assert isinstance(case["visible_length"], int) and "code" not in case, case["name"]
        elif "code" in case:
            assert case["code"] in CODES, case["name"]
    assert {case["code"] for case in cases if "code" in case} == CODES


@pytest.mark.parametrize("case", [c for c in shared_cases() if c["ok"]], ids=lambda case: case["name"])
def test_a_shared_valid_example_is_accepted_with_its_visible_length(case: dict[str, Any]) -> None:
    length, error = run(case["input"])

    assert error is None, f"refused with {error.code} at {error.position}"
    assert length == case["visible_length"]


@pytest.mark.parametrize("case", [c for c in shared_cases() if not c["ok"]], ids=lambda case: case["name"])
def test_a_shared_invalid_example_is_refused_with_its_code(case: dict[str, Any]) -> None:
    error = refusal(case["input"])

    assert error.code in CODES
    if "code" in case:  # у примеров без кода (запрещённые символы, голый «<») план код не фиксирует
        assert error.code == case["code"]


# --- что принимается: серверные примеры ----------------------------------------------------------------------------

VALID: list[tuple[str, str, int]] = [
    ("single quotes in href", "<a href='https://example.org/x'>y</a>", 1),
    ("link with bold inside", '<a href="https://example.org"><b>bold link</b></a>', 9),
    ("deep but valid nesting", "<b><i><u><s>deep</s></u></i></b>", 4),
    ("entities inside code", "<code>1 &lt; 2 &amp;&amp; 3 &gt; 2</code>", 14),
    ("entities inside pre", "<pre>if (a &lt; b) {}</pre>", 13),
    ("spoiler inside quote", "<blockquote><tg-spoiler>x</tg-spoiler></blockquote>", 1),
    ("bold and plain text around", "до <b>после</b> и ещё", 14),
    ("emoji next to text", "a😀b", 4),
    ("zero width joiner alone inside text", f"a{ZWJ}b", 3),
    ("cyrillic with yo", "Ёлка и ёж", 9),
    ("many newlines", "a\n\n\nb", 5),
]


@pytest.mark.parametrize(("text", "length"), [(t, n) for _, t, n in VALID], ids=[name for name, _, _ in VALID])
def test_a_valid_text_is_accepted_with_its_visible_length(text: str, length: int) -> None:
    assert run(text) == (length, None)


def test_the_text_is_never_changed_the_checker_only_measures() -> None:
    from modules.bot.telegram_html import check_telegram_html

    text = "<b>x</b> &amp; y"

    assert isinstance(check_telegram_html(text), int) and text == "<b>x</b> &amp; y"


# --- что отвергается: серверные примеры ----------------------------------------------------------------------------

INVALID: list[tuple[str, str, str]] = [
    # теги
    ("div", "<div>x</div>", "telegram_html_tag"),
    ("br", "line<br>break", "telegram_html_tag"),
    ("paragraph", "<p>x</p>", "telegram_html_tag"),
    ("closing tag of an unknown element", "x</div>", "telegram_html_tag"),
    ("iframe", '<iframe src="https://example.org"></iframe>', "telegram_html_tag"),
    # атрибуты
    ("link without href", "<a>x</a>", "telegram_html_attr"),
    ("class on bold", '<b class="x">x</b>', "telegram_html_attr"),
    ("span without class", "<span>x</span>", "telegram_html_attr"),
    ("span with another class", '<span class="other">x</span>', "telegram_html_attr"),
    ("javascript with leading spaces", '<a href="  javascript:alert(1)">x</a>', "telegram_html_attr"),
    ("javascript in mixed case", '<a href="JaVaScRiPt:alert(1)">x</a>', "telegram_html_attr"),
    ("data url", '<a href="data:text/html,x">x</a>', "telegram_html_attr"),
    ("tg deep link", '<a href="tg://user?id=1">x</a>', "telegram_html_attr"),
    ("scheme-relative url", '<a href="//example.org">x</a>', "telegram_html_attr"),
    ("mailto", '<a href="mailto:a@example.org">x</a>', "telegram_html_attr"),
    ("extra attribute on link", '<a href="https://example.org" target="_blank">x</a>', "telegram_html_attr"),
    ("code class outside pre", '<code class="language-python">x</code>', "telegram_html_attr"),
    ("code class that is not a language", '<pre><code class="bad">x</code></pre>', "telegram_html_attr"),
    ("class on pre", '<pre class="language-python">x</pre>', "telegram_html_attr"),
    ("attribute on blockquote", '<blockquote cite="x">x</blockquote>', "telegram_html_attr"),
    ("attribute on spoiler", '<tg-spoiler class="x">x</tg-spoiler>', "telegram_html_attr"),
    # сущности
    ("unfinished named entity", "Tom &amp Jerry", "telegram_html_entity"),
    ("bad hex entity", "&#xZZ;", "telegram_html_entity"),
    ("lt without semicolon", "a &lt b", "telegram_html_entity"),
    ("less-than before a digit", "love <3 you", "telegram_html_entity"),
    # вложенность
    ("tag closed by another", "<b>x</i>", "telegram_html_nesting"),
    ("lone closing tag", "ok</b>", "telegram_html_nesting"),
    ("code inside code", "<code><code>x</code></code>", "telegram_html_nesting"),
    ("markup inside code", "<code><i>x</i></code>", "telegram_html_nesting"),
    ("markup inside pre with code", '<pre><code class="language-py"><b>x</b></code></pre>', "telegram_html_nesting"),
    (
        "link inside link through bold",
        '<a href="https://a.example"><b><a href="https://b.example">x</a></b></a>',
        "telegram_html_nesting",
    ),
    # пустота
    ("spaces only", "   ", "telegram_html_empty"),
    ("newline and tab only", "\n\t", "telegram_html_empty"),
    ("empty bold with a space", "<b> </b>", "telegram_html_empty"),
    ("empty spoiler", "<tg-spoiler></tg-spoiler>", "telegram_html_empty"),
]


@pytest.mark.parametrize(("text", "code"), [(t, c) for _, t, c in INVALID], ids=[name for name, _, _ in INVALID])
def test_an_invalid_text_is_refused_with_its_code(text: str, code: str) -> None:
    assert refusal(text).code == code


FORBIDDEN_CHARACTERS = [
    ("nul", "a\x00b"),
    ("escape", "a\x1bb"),
    ("carriage return", "a\r\nb"),
    ("right-to-left override", f"a{RLO}b"),
    ("right-to-left embedding", f"a{RLE}b{PDF}"),
    ("left-to-right isolate", f"a{LRI}b{PDI}"),
    ("first strong isolate", f"a{FSI}b{PDI}"),
    ("override written as a numeric entity", "a&#8238;b"),
    ("override written as a hex entity", "a&#x202E;b"),
    ("override inside a tag", f"<b>a{RLO}b</b>"),
]


@pytest.mark.parametrize(("text",), [(t,) for _, t in FORBIDDEN_CHARACTERS], ids=[n for n, _ in FORBIDDEN_CHARACTERS])
def test_control_and_bidi_override_characters_are_refused_even_when_hidden_in_an_entity(text: str) -> None:
    assert refusal(text).code == "telegram_html_entity"


def test_a_scheme_hidden_by_a_named_entity_is_still_refused() -> None:
    error = refusal('<a href="javascript&colon;alert(1)">x</a>')

    assert error.code in {"telegram_html_attr", "telegram_html_entity"}


def test_a_zero_width_joiner_is_allowed_otherwise_family_emoji_would_break() -> None:
    assert run("👨" + ZWJ + "👩" + ZWJ + "👧") == (8, None)


# --- пределы -------------------------------------------------------------------------------------------------------


def test_the_visible_length_counts_utf16_units_not_characters() -> None:
    assert run("😀" * 2048) == (MAX_VISIBLE, None)
    assert refusal("😀" * 2048 + "a").code == "telegram_html_length"


def test_markup_and_entities_do_not_count_towards_the_visible_length() -> None:
    text = "<b>" + "a" * MAX_VISIBLE + "</b>"

    assert run(text) == (MAX_VISIBLE, None)  # исходник длиннее 4096, видимого текста ровно 4096


def test_an_entity_counts_as_what_it_stands_for() -> None:
    assert run("&lt;" * MAX_VISIBLE) == (MAX_VISIBLE, None)
    assert refusal("&lt;" * (MAX_VISIBLE + 1)).code == "telegram_html_length"


def test_the_source_is_limited_to_16384_characters_even_when_little_is_visible() -> None:
    padding = "<b></b>"
    fits = "a" + padding * ((MAX_SOURCE - 1) // len(padding))
    assert len(fits) <= MAX_SOURCE and run(fits) == (1, None)

    too_long = fits + padding
    assert len(too_long) > MAX_SOURCE
    assert refusal(too_long).code == "telegram_html_length"


def test_a_very_deep_nesting_never_crashes_the_checker() -> None:
    text = "<b>" * 2000 + "x" + "</b>" * 2000

    length, error = run(text)  # принят или отвергнут - решает реализация, но RecursionError недопустим

    assert (length, error.code if error else None) in {(1, None), (None, "telegram_html_nesting")}


# --- позиция ошибки ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("<b>ok</b><script>", 9),
        ("ab&nbsp;cd", 2),
        ("ok </b>", 3),
        ('ok <a href="javascript:x()">y</a>', 3),
    ],
    ids=["unknown tag", "unknown entity", "lone closing tag", "bad href"],
)
def test_the_error_points_at_the_offending_token(text: str, position: int) -> None:
    assert refusal(text).position == position
