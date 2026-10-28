"""Бот (этап 25.1), чистые функции: схемы ввода, код приглашения, статус приглашения, платёжная политика.

Ввод из Telegram недоверенный: длины, управляющие символы, почта. Всё это отсекают схемы, до БД и SSH не доходит.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.bot.models.invite import BotInvite
from modules.bot.models.registration import BotRegistration
from modules.bot.schemas import HeartbeatRequest, InviteCreate
from tests.bot_support import (
    EMAIL,
    EXPIRED,
    NAME,
    REVOKED,
    TELEGRAM_ID,
    UNKNOWN,
    USED,
    VALID,
    InviteFree,
    InviteStatus,
    PaymentStatus,
    RegistrationCreate,
    generate_code,
    invite_status,
    status_value,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def valid_fields(**override: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "code": "abcDEF123_-xyz",
        "telegram_id": TELEGRAM_ID,
        "telegram_username": "ivan_p",
        "name": NAME,
        "email": EMAIL,
    }
    return {**fields, **override}


def accepts(**override: Any) -> bool:
    try:
        RegistrationCreate(**valid_fields(**override))
    except ValidationError:
        return False
    return True


# --- RegistrationCreate --------------------------------------------------------------------------------------------


def test_a_normal_registration_is_accepted() -> None:
    data = RegistrationCreate(**valid_fields())
    assert (data.name, data.telegram_id, data.email) == (NAME, TELEGRAM_ID, EMAIL)


def test_telegram_username_is_optional() -> None:
    assert accepts(telegram_username=None)
    fields = valid_fields()
    del fields["telegram_username"]
    assert RegistrationCreate(**fields).telegram_username is None


@pytest.mark.parametrize("name", ["a", "Иван", "Иван Петров", "x" * 64])
def test_names_of_one_to_sixty_four_characters_are_accepted(name: str) -> None:
    assert accepts(name=name)


@pytest.mark.parametrize("name", ["", "x" * 65, "Иван" * 20])
def test_names_outside_one_to_sixty_four_characters_are_refused(name: str) -> None:
    assert not accepts(name=name)


@pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\rb", "a\tb", "a\x1bb", "a\x7fb", "\x00"])
def test_control_characters_are_refused_in_the_name(bad: str) -> None:
    assert not accepts(name=bad)


@pytest.mark.parametrize("bad", ["a‮b", "a​b", "a b", "a b"])
def test_invisible_and_direction_characters_are_refused_in_the_name(bad: str) -> None:
    """RLO, нулевая ширина, разделители строк: по ним имя на экране админа можно подменить."""
    assert not accepts(name=bad)


@pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\x1bb", "x" * 65])
def test_telegram_username_refuses_control_characters_and_long_values(bad: str) -> None:
    assert not accepts(telegram_username=bad)


@pytest.mark.parametrize("email", ["plainaddress", "@example.org", "a@@example.org", "a b@example.org", ""])
def test_malformed_email_is_refused(email: str) -> None:
    assert not accepts(email=email)


def test_email_longer_than_the_user_column_is_refused() -> None:
    assert not accepts(email="a" * 90 + "@example.org")  # vpn_users.email — VARCHAR(100)


@pytest.mark.parametrize("telegram_id", [1, 2**52, 2**63 - 1])
def test_telegram_ids_inside_bigint_are_accepted(telegram_id: int) -> None:
    assert accepts(telegram_id=telegram_id)


@pytest.mark.parametrize("telegram_id", [0, -1, 2**63])
def test_telegram_ids_outside_positive_bigint_are_refused(telegram_id: int) -> None:
    assert not accepts(telegram_id=telegram_id)


@pytest.mark.parametrize("code", ["", "x" * 65])
def test_invite_code_must_fit_the_telegram_start_parameter(code: str) -> None:
    assert not accepts(code=code)


def test_a_sixty_four_character_code_is_accepted() -> None:
    assert accepts(code="x" * 64)


def test_missing_fields_are_refused() -> None:
    for field in ("code", "telegram_id", "name", "email"):
        fields = valid_fields()
        del fields[field]
        with pytest.raises(ValidationError):
            RegistrationCreate(**fields)


@pytest.mark.parametrize("name", ["tollwire_bot", "@tollwire_bot", "A1_b2"])
def test_heartbeat_accepts_telegram_bot_names(name: str) -> None:
    assert HeartbeatRequest(bot_username=name, version="0.1.0").bot_username == name


@pytest.mark.parametrize("name", ["", "evil/path?x=#", "a b", "bot\nname", "bot.name", "x" * 33, "@@bot"])
def test_heartbeat_refuses_names_that_could_change_the_invite_link(name: str) -> None:
    with pytest.raises(ValidationError):
        HeartbeatRequest(bot_username=name, version="0.1.0")


# --- InviteCreate --------------------------------------------------------------------------------------------------


def invite_fields(**override: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"protocol_id": uuid4(), "days": 30, "expires_at": NOW + timedelta(days=1)}
    return {**fields, **override}


@pytest.mark.parametrize("days", [1, 30, 3650])
def test_invite_days_inside_the_range_are_accepted(days: int) -> None:
    assert InviteCreate(**invite_fields(days=days)).days == days


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_invite_days_outside_one_to_3650_are_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        InviteCreate(**invite_fields(days=days))


@pytest.mark.parametrize("missing", ["protocol_id", "days"])
def test_invite_may_leave_protocol_and_days_to_the_settings(missing: str) -> None:
    """С 25.4 протокол и срок необязательны: недостающее берётся из настроек бота (тесты - в test_bot_settings)."""
    fields = invite_fields()
    del fields[missing]
    assert getattr(InviteCreate(**fields), missing) is None


# --- код приглашения -----------------------------------------------------------------------------------------------


def test_generated_code_fits_the_telegram_start_parameter() -> None:
    """start-параметр deep link: до 64 символов из A-Za-z0-9_-."""
    codes = {generate_code() for _ in range(100)}
    assert len(codes) == 100
    for code in codes:
        assert re.fullmatch(r"[A-Za-z0-9_-]{16,64}", code), len(code)


# --- статус приглашения --------------------------------------------------------------------------------------------


def invite(**override: Any) -> BotInvite:
    fields: dict[str, Any] = {
        "code_hash": "h" * 64,
        "protocol_id": uuid4(),
        "days": 30,
        "expires_at": NOW + timedelta(days=1),
        "used_at": None,
        "revoked_at": None,
    }
    return BotInvite(**{**fields, **override})


def state(item: BotInvite) -> str:
    return status_value(invite_status(item, NOW))


def test_a_fresh_invite_is_valid() -> None:
    assert state(invite()) == VALID == status_value(InviteStatus("valid"))


def test_an_invite_is_valid_up_to_its_deadline_and_expired_at_it() -> None:
    assert state(invite(expires_at=NOW + timedelta(seconds=1))) == VALID
    assert state(invite(expires_at=NOW)) == EXPIRED  # срок «до» не включает сам момент
    assert state(invite(expires_at=NOW - timedelta(seconds=1))) == EXPIRED


def test_a_used_invite_is_used() -> None:
    assert state(invite(used_at=NOW - timedelta(hours=1))) == USED


def test_a_used_invite_stays_used_after_its_deadline() -> None:
    """Использованное приглашение не превращается в «истёкшее»: бот скажет человеку верную причину."""
    assert state(invite(used_at=NOW - timedelta(days=2), expires_at=NOW - timedelta(days=1))) == USED


def test_a_revoked_invite_is_revoked() -> None:
    assert state(invite(revoked_at=NOW - timedelta(hours=1))) == REVOKED


def test_all_known_statuses_are_distinct_strings() -> None:
    values = {status_value(item) for item in InviteStatus}
    assert {VALID, USED, EXPIRED, REVOKED} <= values
    assert UNKNOWN not in {VALID, USED, EXPIRED, REVOKED}


# --- шаг оплаты ----------------------------------------------------------------------------------------------------


async def test_invite_free_always_says_paid() -> None:
    registration = BotRegistration(telegram_id=TELEGRAM_ID, name=NAME, email=EMAIL)
    assert await InviteFree().check(registration) == PaymentStatus.PAID
