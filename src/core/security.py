#!/usr/bin/python
# -*- coding: utf-8 -*-

import hashlib
import secrets
from datetime import datetime, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet
from jwt.exceptions import InvalidTokenError

from core.config import settings
from core.utils import get_access_expires_at

ph = PasswordHasher()
fernet = Fernet(settings.encryption_key.get_secret_value())


def hash_password(password: str) -> str:
    return ph.hash(password)


# Хэш случайного пароля: проверяется вместо настоящего, если админа с таким email нет. Тогда ответ
# «нет такого email» идёт столько же, сколько «неверный пароль» (Argon2 ~десятки мс), и по времени
# ответа нельзя узнать, существует ли учётка (тайминг-атака на перечисление пользователей).
DUMMY_PASSWORD_HASH = ph.hash(secrets.token_urlsafe(32))


def verify_password(password: str, hashed: str) -> bool:
    try:
        ph.verify(hashed, password)
    except VerifyMismatchError:
        return False
    return True


def create_access_token(subject: str) -> str:
    payload = {
        "sub": subject,
        "exp": get_access_expires_at(),
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret_key.get_secret_value(), algorithm="HS256")


def decode_access_token(token: str) -> dict[str, int | str] | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key.get_secret_value(), algorithms=["HS256"])
    except InvalidTokenError:
        return None
    return payload


def generate_token(nbytes: int = 32) -> str:
    """Случайный секрет (refresh-токен, ключ бота, код приглашения): URL-безопасный, в БД хранится только hash_token."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


generate_refresh_token = generate_token
hash_refresh_token = hash_token


def encrypt_secret(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()
