"""Схемы этапа 25.4: рассылка, подтверждение отправки, настройки бота, необязательные поля приглашения.

Ошибки Telegram HTML выходят из валидатора схемы как `PydanticCustomError`: код в `type`, позиция в `ctx`."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

MAX_SELECTED = 1000
BOT_ERROR_CODES = ["blocked", "chat_not_found", "bad_request", "network", "telegram_5xx", "flood", "too_many_attempts"]


def broadcast(**fields: Any) -> Any:
    from modules.bot.schemas import BroadcastCreate

    values: dict[str, Any] = {"request_id": uuid4(), "kind": "news", "text": "Привет", "target": "all"}
    return BroadcastCreate(**{**values, **fields})


def refused(**fields: Any) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        broadcast(**fields)
    return caught.value


def error_types(error: ValidationError) -> list[str]:
    return [item["type"] for item in error.errors()]


# --- рассылка: что принимается -------------------------------------------------------------------------------------


def test_news_for_everyone_is_accepted() -> None:
    assert broadcast().target == "all"


def test_news_for_selected_people_is_accepted() -> None:
    assert len(broadcast(target="selected", user_ids=[uuid4(), uuid4()]).user_ids) == 2


def test_news_for_a_protocol_is_accepted() -> None:
    protocol_id = uuid4()

    assert broadcast(target="protocol", protocol_id=protocol_id).protocol_id == protocol_id


@pytest.mark.parametrize(
    "fields",
    [{"target": "protocol", "protocol_id": uuid4()}, {"target": "selected", "user_ids": [uuid4()]}],
    ids=["protocol", "selected"],
)
def test_config_links_go_to_a_protocol_or_to_selected_people(fields: dict[str, Any]) -> None:
    assert broadcast(kind="config", text=None, **fields).kind == "config"


def test_a_thousand_selected_people_is_the_limit() -> None:
    assert len(broadcast(target="selected", user_ids=[uuid4() for _ in range(MAX_SELECTED)]).user_ids) == MAX_SELECTED


# --- рассылка: что отвергается -------------------------------------------------------------------------------------


INVALID_BROADCASTS: list[tuple[dict[str, Any], str]] = [
    ({"kind": "digest"}, "unknown kind"),
    ({"kind": "broadcast"}, "the old name of the kind from the technical specification"),
    ({"target": "everybody"}, "unknown target"),
    ({"text": None}, "news without text"),
    ({"text": ""}, "news with empty text"),
    ({"kind": "config", "text": None, "target": "all"}, "config links to everybody"),
    ({"target": "protocol"}, "protocol target without a protocol"),
    ({"target": "protocol", "protocol_id": "not-a-uuid"}, "protocol id is not a uuid"),
    ({"target": "selected"}, "selected without people"),
    ({"target": "selected", "user_ids": []}, "selected with an empty list"),
    ({"target": "selected", "user_ids": ["not-a-uuid"]}, "person id is not a uuid"),
    ({"target": "selected", "user_ids": [uuid4() for _ in range(MAX_SELECTED + 1)]}, "more than a thousand people"),
    ({"request_id": None}, "no request id"),
    ({"request_id": "not-a-uuid"}, "request id is not a uuid"),
]


@pytest.mark.parametrize(("fields", "why"), INVALID_BROADCASTS, ids=[why for _, why in INVALID_BROADCASTS])
def test_an_invalid_broadcast_is_refused(fields: dict[str, Any], why: str) -> None:
    refused(**fields)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("<script>alert(1)</script>", "telegram_html_tag"),
        ('<a href="javascript:alert(1)">x</a>', "telegram_html_attr"),
        ("Tom & Jerry", "telegram_html_entity"),
        ("<b>x", "telegram_html_nesting"),
        ("a" * 4097, "telegram_html_length"),
        ("<b></b>", "telegram_html_empty"),
    ],
    ids=["tag", "attr", "entity", "nesting", "length", "empty"],
)
def test_bad_news_text_fails_with_the_html_error_code_and_a_position(text: str, code: str) -> None:
    (item,) = refused(text=text).errors()

    assert item["type"] == code
    assert item["loc"] == ("text",)
    assert isinstance(item["ctx"]["position"], int)


def test_the_text_of_a_config_broadcast_is_not_checked_as_html() -> None:
    """У `config` текста нет: сообщение собирает бот из своего шаблона."""
    assert broadcast(kind="config", text=None, target="protocol", protocol_id=uuid4()).text is None


def test_news_without_text_is_a_missing_field_not_an_html_error() -> None:
    assert "telegram_html_empty" not in error_types(refused(text=None))  # нет текста - это «поле нужно», а не «пусто»


# --- подтверждение отправки (бот -> панель) ------------------------------------------------------------------------


def ack_item(**fields: Any) -> Any:
    from modules.bot.schemas import OutboxAckItem

    return OutboxAckItem(**{"id": uuid4(), "status": "sent", **fields})


@pytest.mark.parametrize("status", ["sent", "blocked", "failed", "retry"])
def test_the_bot_may_report_these_outcomes(status: str) -> None:
    assert ack_item(status=status).status == status


@pytest.mark.parametrize("status", ["pending", "claimed", "skipped", "cancelled", "done", ""])
def test_the_bot_may_not_report_other_statuses(status: str) -> None:
    with pytest.raises(ValidationError):
        ack_item(status=status)


@pytest.mark.parametrize("code", BOT_ERROR_CODES)
def test_the_error_is_a_code_from_the_list(code: str) -> None:
    assert ack_item(status="failed", error=code).error == code


@pytest.mark.parametrize("text", ["Bad Request: chat not found", "Forbidden: bot was blocked by the user", "x" * 33])
def test_the_text_of_a_telegram_error_is_refused_only_codes_pass(text: str) -> None:
    with pytest.raises(ValidationError):
        ack_item(status="failed", error=text)


def test_retry_after_may_not_be_negative() -> None:
    with pytest.raises(ValidationError):
        ack_item(status="retry", retry_after=-1)


def test_error_and_retry_after_are_optional() -> None:
    item = ack_item()

    assert item.error is None and item.retry_after is None


# --- настройки и приглашение ---------------------------------------------------------------------------------------


def settings_update(**fields: Any) -> Any:
    from modules.bot.schemas import BotSettingsUpdate

    return BotSettingsUpdate(**{"default_protocol_id": None, "default_days": 30, "invite_valid_days": 7, **fields})


@pytest.mark.parametrize("days", [1, 3650])
def test_default_days_at_the_edges_are_accepted(days: int) -> None:
    assert settings_update(default_days=days).default_days == days


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_default_days_outside_the_range_are_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        settings_update(default_days=days)


@pytest.mark.parametrize("days", [1, 365])
def test_invite_validity_at_the_edges_is_accepted(days: int) -> None:
    assert settings_update(invite_valid_days=days).invite_valid_days == days


@pytest.mark.parametrize("days", [0, -1, 366])
def test_invite_validity_outside_the_range_is_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        settings_update(invite_valid_days=days)


def test_the_default_protocol_may_be_cleared() -> None:
    assert settings_update(default_protocol_id=None).default_protocol_id is None


def test_an_invite_can_be_asked_for_without_protocol_days_and_deadline() -> None:
    from modules.bot.schemas import InviteCreate

    data = InviteCreate()

    assert (data.protocol_id, data.days, data.expires_at) == (None, None, None)


def test_an_invite_still_checks_the_values_it_is_given() -> None:
    from modules.bot.schemas import InviteCreate

    assert InviteCreate(days=1).days == 1  # одно только поле принимается
    with pytest.raises(ValidationError):
        InviteCreate(days=0)
    with pytest.raises(ValidationError):
        InviteCreate(expires_at="2030-01-01T00:00:00")  # без часового пояса
