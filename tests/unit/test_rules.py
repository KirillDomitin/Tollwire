"""Бизнес-правила без БД: «доступ живой?», имя клиента, аудит-помощники, безопасность."""

import enum
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.security import (
    create_access_token,
    decode_access_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from modules.access.models import AccessGrantStatus
from modules.access.service import CLIENT_NAME_PREFIX, client_name, inactive_reason
from modules.audit.service import _to_json, diff_changes

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
S = AccessGrantStatus


def access(grant: S = S.ACTIVE, sub: S = S.ACTIVE, days_from: int = -1, days_until: int = 1):  # type: ignore[no-untyped-def]
    grant_obj = SimpleNamespace(status=grant)
    sub_obj = SimpleNamespace(
        status=sub, valid_from=NOW + timedelta(days=days_from), valid_until=NOW + timedelta(days=days_until)
    )
    return grant_obj, sub_obj


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"grant": S.PENDING, "sub": S.PENDING}, None),  # в периоде статус PENDING не мешает — решают даты
        ({"grant": S.REVOKED}, "Access grant is revoked"),
        ({"grant": S.EXPIRED}, "Access grant is expired"),
        ({"sub": S.REVOKED}, "Subscription is not active"),
        ({"days_from": 1, "days_until": 2}, "Subscription period has not started yet"),
        ({"days_from": -2, "days_until": -1}, "Subscription period has ended"),
        ({"days_from": -2, "days_until": 0}, "Subscription period has ended"),  # valid_until == сейчас — уже конец
    ],
)
def test_inactive_reason(kwargs: dict[str, object], expected: str | None) -> None:
    grant, subscription = access(**kwargs)  # type: ignore[arg-type]
    assert inactive_reason(grant, subscription, NOW) == expected  # type: ignore[arg-type]


def test_client_name_is_ours_and_safe() -> None:
    name = client_name(uuid4())
    assert name.startswith(CLIENT_NAME_PREFIX)
    assert len(name) <= 32 and name.replace("-", "").isalnum()


def test_diff_changes_only_real_changes() -> None:
    obj = SimpleNamespace(name="a", host="h", port=22)
    assert diff_changes(obj, {"name": "a", "host": "h2", "port": 22}) == {"host": ["h", "h2"]}
    assert diff_changes(obj, {"name": "a"}) == {}


def test_to_json_makes_metadata_serializable() -> None:
    class Color(enum.Enum):
        RED = "red"

    uid = uuid4()
    assert _to_json({"id": uid, "at": NOW, "c": Color.RED, "nested": [{"x": uid}], "n": 1}) == {
        "id": str(uid),
        "at": NOW.isoformat(),
        "c": "red",
        "nested": [{"x": str(uid)}],
        "n": 1,
    }


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse")
    assert hashed.startswith("$argon2id$")
    assert verify_password("correct horse", hashed)
    assert not verify_password("correct horsE", hashed)


def test_access_token_roundtrip_and_tamper() -> None:
    token = create_access_token("admin-id")
    payload = decode_access_token(token)
    assert payload is not None and payload["sub"] == "admin-id" and payload["type"] == "access"
    assert decode_access_token(token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]) is None
    assert decode_access_token("not.a.jwt") is None


def test_secret_encryption_roundtrip() -> None:
    token = encrypt_secret("ssh-password")
    assert "ssh-password" not in token
    assert token != encrypt_secret("ssh-password")  # каждый раз разный шифротекст
    assert decrypt_secret(token) == "ssh-password"


def test_refresh_token_hash_is_stable_and_not_the_token() -> None:
    assert hash_refresh_token("abc") == hash_refresh_token("abc") != "abc"
