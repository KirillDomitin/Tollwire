#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Создание администратора панели (первый вход в свежую установку).

    cd src && uv run python -m scripts.create_admin admin@example.com   # локально
    docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.create_admin \
        admin@example.com

Роль по умолчанию — superadmin: скрипт нужен прежде всего, чтобы завести первого управляющего.
--role operator|viewer — для остальных (их удобнее создавать через POST /api/v1/admins).

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
from modules.auth.models.admin import Admin, AdminRole

MIN_PASSWORD_LENGTH = 12


def read_password() -> str:
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("Passwords do not match")
    return password


async def create_admin(email: str, password: str, role: AdminRole) -> None:
    async with session_factory() as session:
        if await session.scalar(select(Admin.id).where(Admin.email == email)):
            sys.exit(f"Admin {email} already exists")
        session.add(Admin(email=email, password_hash=hash_password(password), role=role))
        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a panel administrator")
    parser.add_argument("email")
    parser.add_argument("--role", choices=[role.value for role in AdminRole], default=AdminRole.SUPERADMIN.value)
    args = parser.parse_args()
    try:
        email = str(TypeAdapter(EmailStr).validate_python(args.email))
    except ValidationError:
        sys.exit(f"Invalid email: {args.email}")
    role = AdminRole(args.role)
    asyncio.run(create_admin(email, read_password(), role))
    print(f"Admin {email} created with role {role.value}")


if __name__ == "__main__":
    main()
