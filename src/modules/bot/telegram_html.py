#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Проверка текста новости в формате Telegram HTML: строгий белый список, отказ вместо правки.

Текст молча не меняется: админ видит в предпросмотре то же, что получат люди, а всё, что Telegram отверг бы или
понял иначе, отвергается здесь с кодом и позицией. Свой разборщик, а не `html.parser`: он прощает `<3` и незакрытые
теги, а Telegram - нет. Правила совпадают с `frontend/src/lib/telegramHtml.ts`; общий набор примеров -
`frontend/src/lib/telegramHtml.cases.json`."""

import re

MAX_VISIBLE = 4096  # видимый текст в единицах UTF-16, как считает Telegram (после разбора разметки)
MAX_SOURCE = 16384  # исходник вместе с разметкой

TAG = "telegram_html_tag"
ATTR = "telegram_html_attr"
ENTITY = "telegram_html_entity"
NESTING = "telegram_html_nesting"
LENGTH = "telegram_html_length"
EMPTY = "telegram_html_empty"

_PLAIN_TAGS = frozenset({"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "tg-spoiler"})
_NAMED_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"'}
_ENTITY = re.compile(r"&(?:#([0-9]{1,7})|#[xX]([0-9A-Fa-f]{1,6})|([A-Za-z]{1,8}));")
# Пробельные символы в разметке - явный набор, а не \s: у Python и JavaScript он разный (\x1c-\x1f, \x85, U+FEFF), и
# проверка на сервере разошлась бы с предпросмотром.
_WS = r"[ \t\n\r\f]"
_NOT_WS = r"[^ \t\n\r\f"
_OPEN_TAG = re.compile(
    rf"<([A-Za-z][A-Za-z0-9-]*)((?:{_WS}+{_NOT_WS}=>/\"'<]+(?:{_WS}*={_WS}*(?:\"[^\"]*\"|'[^']*'))?)*){_WS}*>"
)
_CLOSE_TAG = re.compile(rf"</([A-Za-z][A-Za-z0-9-]*){_WS}*>")
_ATTRIBUTE = re.compile(rf"""({_NOT_WS}=>/"'<]+)(?:{_WS}*={_WS}*(?:"([^"]*)"|'([^']*)'))?""")
_TAG_NAME = re.compile(r"<([A-Za-z][A-Za-z0-9-]*)(?=[ \t\n\r\f/>])")
_KNOWN_TAGS = _PLAIN_TAGS | {"span", "a", "code", "pre", "blockquote"}
_LANGUAGE = re.compile(r"language-[A-Za-z0-9_+#.-]{1,32}")
_FORBIDDEN_BIDI = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))


class TelegramHtmlError(ValueError):
    """Текст не годится для Telegram: `code` - одно из telegram_html_*, `position` - индекс символа в исходнике.
    В сообщении ни кусочка текста: он уйдёт в ответ API и в логи."""

    def __init__(self, code: str, position: int):
        super().__init__(f"{code} at {position}")
        self.code = code
        self.position = position


def _forbidden(char: str) -> bool:
    code = ord(char)
    if char in "\n\t":
        return False
    return code < 0x20 or 0x7F <= code <= 0x9F or code in _FORBIDDEN_BIDI


def _units(text: str) -> int:
    """Длина в единицах UTF-16: символы вне BMP (эмодзи) - две."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _decode(text: str, offset: int) -> str:
    """Подставить сущности (только &lt; &gt; &amp; &quot; и числовые); всё прочее с «&» - ошибка."""
    result: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "&":
            if _forbidden(char):
                raise TelegramHtmlError(ENTITY, offset + index)
            result.append(char)
            index += 1
            continue
        match = _ENTITY.match(text, index)
        if match is None:
            raise TelegramHtmlError(ENTITY, offset + index)
        decimal, hexadecimal, named = match.groups()
        if named is not None:
            if named not in _NAMED_ENTITIES:
                raise TelegramHtmlError(ENTITY, offset + index)
            result.append(_NAMED_ENTITIES[named])
        else:
            number = int(decimal) if decimal is not None else int(hexadecimal, 16)
            if number == 0 or number > 0x10FFFF or 0xD800 <= number <= 0xDFFF:
                raise TelegramHtmlError(ENTITY, offset + index)
            char = chr(number)
            if _forbidden(char):  # управляющий или «переворачивающий» символ, спрятанный в сущности
                raise TelegramHtmlError(ENTITY, offset + index)
            result.append(char)
        index = match.end()
    return "".join(result)


def _attributes(raw: str, position: int) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for match in _ATTRIBUTE.finditer(raw):
        name = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        if name in found:
            raise TelegramHtmlError(ATTR, position)
        found[name] = None if value is None else _decode(value, position)
    return found


def _check_attributes(name: str, attrs: dict[str, str | None], parent: str | None, position: int) -> None:
    """Допустимые атрибуты каждого тега: любое другое - отказ."""
    if name in _PLAIN_TAGS or name == "pre":
        if attrs:
            raise TelegramHtmlError(ATTR, position)
    elif name == "span":
        if attrs != {"class": "tg-spoiler"}:
            raise TelegramHtmlError(ATTR, position)
    elif name == "a":
        href = attrs.get("href")
        if set(attrs) != {"href"} or href is None:
            raise TelegramHtmlError(ATTR, position)
        if not re.match(
            r"https?://[^ \t\n\r\f]", href, re.IGNORECASE
        ):  # только http/https, без javascript:, data:, tg://
            raise TelegramHtmlError(ATTR, position)
    elif name == "code":
        if attrs:
            language = attrs.get("class")
            if parent != "pre" or set(attrs) != {"class"} or language is None or not _LANGUAGE.fullmatch(language):
                raise TelegramHtmlError(ATTR, position)
    elif name == "blockquote":
        if set(attrs) - {"expandable"} or attrs.get("expandable") not in (None, "", "expandable"):
            raise TelegramHtmlError(ATTR, position)


def _check_opening(name: str, stack: list[str], position: int) -> None:
    parent = stack[-1] if stack else None
    if parent == "code" or (parent == "pre" and name != "code"):
        raise TelegramHtmlError(NESTING, position)  # внутри code и pre разметки нет (в pre допустим один code)
    if name == "a" and "a" in stack:
        raise TelegramHtmlError(NESTING, position)
    if name == "blockquote" and "blockquote" in stack:
        raise TelegramHtmlError(NESTING, position)


def check_telegram_html(text: str) -> int:
    """Видимая длина текста в единицах UTF-16. Текст не годится - `TelegramHtmlError`; сам текст не меняется."""
    if len(text) > MAX_SOURCE:
        raise TelegramHtmlError(LENGTH, MAX_SOURCE)
    stack: list[str] = []
    visible = 0
    has_content = False
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char == "<":
            closing = _CLOSE_TAG.match(text, index)
            if closing is not None:
                name = closing.group(1).lower()
                if name not in _KNOWN_TAGS:
                    raise TelegramHtmlError(TAG, index)
                if not stack or stack[-1] != name:
                    raise TelegramHtmlError(NESTING, index)
                stack.pop()
                index = closing.end()
                continue
            opening = _OPEN_TAG.match(text, index)
            if opening is None:
                named = _TAG_NAME.match(text, index)
                if named is None:
                    raise TelegramHtmlError(ENTITY, index)  # «<» не начало тега: пишется как &lt;
                # Тег есть, но атрибуты не по правилам (без кавычек, «/>»): чужой тег - TAG, свой - ATTR.
                raise TelegramHtmlError(TAG if named.group(1).lower() not in _KNOWN_TAGS else ATTR, index)
            name = opening.group(1).lower()
            if name not in _KNOWN_TAGS:
                raise TelegramHtmlError(TAG, index)
            _check_attributes(name, _attributes(opening.group(2), index), stack[-1] if stack else None, index)
            _check_opening(name, stack, index)
            stack.append(name)
            index = opening.end()
            continue
        end = text.find("<", index)
        chunk = text[index : end if end != -1 else size]
        decoded = _decode(chunk, index)
        visible += _units(decoded)
        has_content = has_content or any(part not in " \t\n\r\f" for part in decoded)
        index += len(chunk)
    if stack:
        raise TelegramHtmlError(NESTING, size)
    if not has_content:
        raise TelegramHtmlError(EMPTY, 0)
    if visible > MAX_VISIBLE:
        raise TelegramHtmlError(LENGTH, 0)
    return visible
