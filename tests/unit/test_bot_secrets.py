"""Общие токены `core.security` (этап 25.1): ключ бота, код приглашения, токены ссылок — один генератор и один хеш."""

import hashlib
import re

from core.security import generate_token, hash_token


def test_hash_token_is_sha256_hex() -> None:
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_hash_token_differs_per_token_and_is_stable() -> None:
    assert hash_token("one") == hash_token("one")
    assert hash_token("one") != hash_token("two")


def test_generated_tokens_are_long_unique_and_url_safe() -> None:
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(token) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", token) for token in tokens)


def test_hash_of_a_generated_token_does_not_contain_it() -> None:
    token = generate_token()
    assert token not in hash_token(token) and len(hash_token(token)) == 64
