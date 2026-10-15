#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Создание администратора панели (первый вход в свежую установку).

    PYTHONPATH=src uv run python -m scripts.create_admin                       # меню: email, затем пароль
    PYTHONPATH=src uv run python -m scripts.create_admin admin@example.com --random-password
    docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.create_admin

Пароль — на выбор:
  * случайный (по умолчанию): показывается на экране один раз, нигде не сохраняется, админ при первом входе обязан
    задать свой (`must_change_password`). Печатается только в терминал — при выводе в файл или через конвейер
    (`| tee install.log`) скрипт отказывается, чтобы пароль не остался в логе;
  * свой: вводится скрыто (getpass), не короче 12 символов, смена не требуется.

--only-if-empty — ничего не делать, если в панели уже есть хоть один админ (повторный запуск установщика).
--role operator|viewer — для остальных ролей (их удобнее создавать через POST /api/v1/admins).
"""

import argparse
import asyncio
import getpass
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select

from core.database.postgres import session_factory
from core.security import hash_password
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.auth.models.admin import Admin, AdminRole

MIN_PASSWORD_LENGTH = 12
# Без похожих символов (l I 1, O 0): такой пароль переписывают с экрана руками. 54 знака × 20 позиций ≈ 115 бит.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_GROUPS = 4
PASSWORD_GROUP_LENGTH = 5


class CliError(Exception):
    """Понятная человеку причина, по которой скрипт не может продолжить (печатается без трассировки)."""


@dataclass
class Console:
    """Всё, что скрипт делает с терминалом, — отдельным объектом: тесты подставляют свой."""

    interactive: bool  # и ввод, и вывод — терминал (можно спрашивать и показывать пароль)
    ask: Callable[[str], str] = field(default=input)
    ask_secret: Callable[[str], str] = field(default=getpass.getpass)
    say: Callable[[str], None] = field(default=print)

    @classmethod
    def system(cls) -> "Console":
        return cls(interactive=sys.stdin.isatty() and sys.stdout.isatty())


def generate_password() -> str:
    groups = (
        "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_GROUP_LENGTH)) for _ in range(PASSWORD_GROUPS)
    )
    return "-".join(groups)


def parse_email(raw: str) -> str:
    try:
        return str(TypeAdapter(EmailStr).validate_python(raw.strip()))
    except ValidationError:
        raise CliError(f"Некорректный email: {raw.strip() or '(пусто)'}") from None


def choose_password(console: Console) -> tuple[str, bool]:
    """(пароль, временный ли он). Меню: случайный по умолчанию или свой."""
    console.say("Пароль администратора:")
    console.say("  1) случайный — будет показан один раз, при первом входе панель попросит задать свой (рекомендуется)")
    console.say("  2) свой")
    while True:
        choice = console.ask("Выбор [1]: ").strip() or "1"
        if choice == "1":
            return generate_password(), True
        if choice == "2":
            return read_own_password(console), False
        console.say("Введите 1 или 2.")


def read_own_password(console: Console) -> str:
    password = console.ask_secret("Пароль: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise CliError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов.")
    if console.ask_secret("Повторите пароль: ") != password:
        raise CliError("Пароли не совпадают.")
    return password


async def count_admins() -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Admin)) or 0)


async def create_admin(email: str, password: str, role: AdminRole, *, must_change_password: bool = False) -> None:
    """Создаёт админа и записывает это в аудит. Пароль в аудит не попадает."""
    async with session_factory() as session:
        if await session.scalar(select(Admin.id).where(Admin.email == email)):
            raise CliError(f"Админ {email} уже существует.")
        admin = Admin(
            email=email, password_hash=hash_password(password), role=role, must_change_password=must_change_password
        )
        session.add(admin)
        await session.flush()
        AuditService(session, SYSTEM_ACTOR).record(
            AuditAction.ADMIN_CREATED,
            AuditEntity.ADMIN,
            admin.id,
            {"email": email, "role": role, "must_change_password": must_change_password, "source": "cli"},
        )
        await session.commit()


def show_generated_password(console: Console, email: str, password: str) -> None:
    console.say("")
    console.say(f"Админ {email} создан.")
    console.say("")
    console.say(f"    Временный пароль:  {password}")
    console.say("")
    console.say("Он показан один раз и нигде не сохранён: запишите его сейчас.")
    console.say("При первом входе панель попросит задать свой пароль.")


async def run(argv: Sequence[str], console: Console) -> None:
    parser = argparse.ArgumentParser(description="Создать администратора панели")
    parser.add_argument("email", nargs="?", help="email админа (не указан — спросим)")
    parser.add_argument("--role", choices=[role.value for role in AdminRole], default=AdminRole.SUPERADMIN.value)
    parser.add_argument("--random-password", action="store_true", help="сгенерировать пароль без вопросов")
    parser.add_argument("--only-if-empty", action="store_true", help="ничего не делать, если админы уже есть")
    args = parser.parse_args(argv)

    if args.only_if_empty and await count_admins() > 0:
        console.say("Админы уже есть — ничего не создаю.")
        return

    raw_email = args.email
    if raw_email is None:
        if not console.interactive:
            raise CliError("Не указан email: передайте его аргументом или запустите в терминале.")
        raw_email = console.ask("Email администратора: ")
    email = parse_email(raw_email)
    role = AdminRole(args.role)

    # Всё, что может отказать, — до создания: недосозданный админ без показанного пароля хуже отказа.
    if args.random_password:
        password, temporary = generate_password(), True
    elif console.interactive:
        password, temporary = choose_password(console)
    else:
        raise CliError("Нет терминала для ввода пароля: добавьте --random-password или запустите в терминале.")
    if temporary and not console.interactive:
        raise CliError(
            "Сгенерированный пароль можно показать только в терминале (не в файле и не через конвейер), "
            "иначе он попадёт в лог. Запустите команду в терминале."
        )

    await create_admin(email, password, role, must_change_password=temporary)
    if temporary:
        show_generated_password(console, email, password)
    else:
        console.say(f"Админ {email} создан с ролью {role.value}.")


def main() -> None:
    try:
        asyncio.run(run(sys.argv[1:], Console.system()))
    except CliError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
