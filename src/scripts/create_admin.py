#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Создание администратора панели (первый вход в свежую установку).

    cd src && uv run python -m scripts.create_admin admin@example.com   # локально
    docker compose -f docker-compose.prod.yml run --rm api python -m scripts.create_admin admin@example.com

Пароль вводится скрыто (getpass): не попадает ни в историю shell, ни в список процессов, ни в логи.
"""

import argparse
import asyncio
import getpass
import sys

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from core.database.postgres import session_factory
from core.security import hash_password
from modules.auth.models.admin import Admin

MIN_PASSWORD_LENGTH = 12


def read_password() -> str:
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("Passwords do not match")
    return password


async def create_admin(email: str, password: str) -> None:
    async with session_factory() as session:
        if await session.scalar(select(Admin.id).where(Admin.email == email)):
            sys.exit(f"Admin {email} already exists")
        session.add(Admin(email=email, password_hash=hash_password(password)))
        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a panel administrator")
    parser.add_argument("email")
    args = parser.parse_args()
    try:
        email = str(TypeAdapter(EmailStr).validate_python(args.email))
    except ValidationError:
        sys.exit(f"Invalid email: {args.email}")
    asyncio.run(create_admin(email, read_password()))
    print(f"Admin {email} created")


if __name__ == "__main__":
    main()
