#!/usr/bin/python
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jwt.exceptions import ExpiredSignatureError, InvalidSignatureError, InvalidTokenError

from core.config import settings

ph = PasswordHasher()


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        ph.verify(hashed, password)
    except VerifyMismatchError:
        return False
    return True


def create_access_token(subject: str) -> str:
    payload = {
        "sub": subject,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_access_expire),
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
