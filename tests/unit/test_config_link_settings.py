"""Страница конфига (этап 25.3), чистые части: формат токена, публичный адрес панели, маскировка `token` в логах."""

import json
import logging
import re

import pytest
from pydantic import ValidationError

from core.config import Settings
from core.log import JsonFormatter, TextFormatter, redact, setup_logging
from core.security import generate_token, hash_token
from tests.bot_support import PUBLIC_ADDRESS_ATTR, TOKEN_SHAPE

SECRET_TOKEN = "Qw3rTy_uIoP-aSdFgHjKlZxCvBnM0123456789abcde"  # 43 символа, как у настоящего токена ссылки


def make_settings(**fields: object) -> Settings:
    """Настройки без .env и без PUBLIC_URL из окружения тестов: адрес задаётся только аргументами."""
    values: dict[str, object] = {"public_url": "", "panel_domain": "", "https_port": 443, **fields}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg,arg-type]


def address(**fields: object) -> str | None:
    value = getattr(make_settings(**fields), PUBLIC_ADDRESS_ATTR)
    return None if not value else str(value)


# --- токен ---------------------------------------------------------------------------------------------------------


def test_the_secret_token_shape_is_43_url_safe_characters() -> None:
    assert len(SECRET_TOKEN) == 43 and TOKEN_SHAPE.fullmatch(SECRET_TOKEN)
    for _ in range(50):
        assert TOKEN_SHAPE.fullmatch(generate_token(32))  # 32 байта -> 43 символа base64url без заполнения


def test_the_database_keeps_a_sha256_of_the_token_not_the_token() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", hash_token(SECRET_TOKEN)) and SECRET_TOKEN not in hash_token(SECRET_TOKEN)


# --- публичный адрес -----------------------------------------------------------------------------------------------


def test_public_url_wins_and_loses_the_trailing_slash() -> None:
    assert address(public_url="https://vpn.example.org/") == "https://vpn.example.org"


def test_the_address_is_built_from_the_domain_and_the_default_https_port() -> None:
    assert address(panel_domain="vpn.example.org") == "https://vpn.example.org"


def test_the_address_is_built_from_the_domain_and_a_custom_https_port() -> None:
    assert address(panel_domain="vpn.example.org", https_port=8443) == "https://vpn.example.org:8443"


def test_public_url_beats_the_domain_and_the_port() -> None:
    assert address(public_url="https://a.example.org", panel_domain="b.example.org", https_port=8443) == (
        "https://a.example.org"
    )


def test_a_public_url_with_a_port_is_kept() -> None:
    assert address(public_url="https://vpn.example.org:8443") == "https://vpn.example.org:8443"


def test_without_url_and_domain_the_address_is_not_set() -> None:
    assert address() is None


@pytest.mark.parametrize("url", ["http://localhost", "http://localhost:8080", "http://127.0.0.1:3000"])
def test_plain_http_is_allowed_for_localhost_only(url: str) -> None:
    assert address(public_url=url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://vpn.example.org",  # токен в ссылке не должен идти открытым текстом
        "ftp://vpn.example.org",
        "vpn.example.org",
        "https://vpn.example.org/panel",  # без пути
        "https://vpn.example.org/?a=1",  # без query
        "https://vpn.example.org/#frag",
        "https://user:pass@vpn.example.org",
        "https://",
    ],
)
def test_a_public_url_that_is_not_a_bare_https_address_is_refused(url: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(public_url=url)


# --- маскировка в логах --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _logging() -> None:
    setup_logging("debug", "text")


def make_record(msg: str, **extra: object) -> logging.LogRecord:
    return logging.getLogger("test").makeRecord("test", logging.INFO, __file__, 1, msg, (), None, extra=extra)


@pytest.mark.parametrize(
    "leak",
    [
        f'{{"token": "{SECRET_TOKEN}"}}',
        f"{{'token': '{SECRET_TOKEN}'}}",
        f"token={SECRET_TOKEN}",
        f"token: {SECRET_TOKEN}",
        f'body {{"token":"{SECRET_TOKEN}"}} failed',
    ],
)
def test_the_token_field_is_redacted(leak: str) -> None:
    assert SECRET_TOKEN not in redact(leak)
    for formatter in (JsonFormatter(), TextFormatter()):
        assert SECRET_TOKEN not in formatter.format(make_record(f"request failed {leak}"))


def test_redacting_the_token_keeps_the_field_name_and_the_json_valid() -> None:
    line = JsonFormatter().format(make_record(f'body {{"token": "{SECRET_TOKEN}", "name": "Ivan"}}'))
    entry = json.loads(line)
    assert "token" in entry["msg"] and "Ivan" in entry["msg"] and SECRET_TOKEN not in entry["msg"]


def test_a_short_token_word_in_ordinary_text_is_left_alone() -> None:
    text = "token refreshed for session 3f2c, 2 tokens left"
    assert redact(text) == text
