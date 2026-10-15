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
import os
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

# Язык сообщений: PANEL_LANG (ru | en), иначе по локали системы (LC_ALL, LC_MESSAGES, LANG), иначе английский.
# Установщик (install.sh) передаёт PANEL_LANG в контейнер, чтобы скрипт говорил на том же языке.
MESSAGES: dict[str, dict[str, str]] = {
    "description": {"ru": "Создать администратора панели", "en": "Create a panel administrator"},
    "email_help": {"ru": "email админа (не указан — спросим)", "en": "admin email (asked if omitted)"},
    "random_help": {"ru": "сгенерировать пароль без вопросов", "en": "generate a password without asking"},
    "empty_help": {"ru": "ничего не делать, если админы уже есть", "en": "do nothing if admins already exist"},
    "invalid_email": {"ru": "Некорректный email: {value}", "en": "Invalid email: {value}"},
    "empty_value": {"ru": "(пусто)", "en": "(empty)"},
    "menu_title": {"ru": "Пароль администратора:", "en": "Administrator password:"},
    "menu_random": {
        "ru": "  1) случайный — будет показан один раз, при первом входе панель попросит задать свой (рекомендуется)",
        "en": "  1) random - shown once, the panel will ask for your own password at first sign-in (recommended)",
    },
    "menu_own": {"ru": "  2) свой", "en": "  2) your own"},
    "menu_choice": {"ru": "Выбор [1]: ", "en": "Choice [1]: "},
    "menu_again": {"ru": "Введите 1 или 2.", "en": "Enter 1 or 2."},
    "password_prompt": {"ru": "Пароль: ", "en": "Password: "},
    "password_repeat": {"ru": "Повторите пароль: ", "en": "Repeat the password: "},
    "password_short": {
        "ru": "Пароль должен быть не короче {min} символов.",
        "en": "The password must be at least {min} characters long.",
    },
    "password_mismatch": {"ru": "Пароли не совпадают.", "en": "The passwords do not match."},
    "exists": {"ru": "Админ {email} уже существует.", "en": "Admin {email} already exists."},
    "created_header": {"ru": "Админ {email} создан.", "en": "Admin {email} created."},
    "temporary_password": {"ru": "    Временный пароль:  {password}", "en": "    Temporary password:  {password}"},
    "shown_once": {
        "ru": "Он показан один раз и нигде не сохранён: запишите его сейчас.",
        "en": "It is shown once and stored nowhere: write it down now.",
    },
    "will_ask_new": {
        "ru": "При первом входе панель попросит задать свой пароль.",
        "en": "The panel will ask you to set your own password at first sign-in.",
    },
    "created_role": {"ru": "Админ {email} создан с ролью {role}.", "en": "Admin {email} created with the {role} role."},
    "admins_exist": {"ru": "Админы уже есть — ничего не создаю.", "en": "Admins already exist - nothing to create."},
    "email_needed": {
        "ru": "Не указан email: передайте его аргументом или запустите в терминале.",
        "en": "No email given: pass it as an argument or run in a terminal.",
    },
    "email_prompt": {"ru": "Email администратора: ", "en": "Administrator email: "},
    "no_terminal_for_password": {
        "ru": "Нет терминала для ввода пароля: добавьте --random-password или запустите в терминале.",
        "en": "No terminal to type a password: add --random-password or run in a terminal.",
    },
    "generated_needs_terminal": {
        "ru": "Сгенерированный пароль можно показать только в терминале (не в файле и не через конвейер), "
        "иначе он попадёт в лог. Запустите команду в терминале.",
        "en": "A generated password can only be shown in a terminal (not to a file or a pipe), otherwise it "
        "would end up in a log. Run the command in a terminal.",
    },
}


def language() -> str:
    for name in ("PANEL_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value:
            return "ru" if value.lower().startswith("ru") else "en"
    return "en"


def t(key: str, **values: object) -> str:
    return MESSAGES[key][language()].format(**values)


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
        raise CliError(t("invalid_email", value=raw.strip() or t("empty_value"))) from None


def choose_password(console: Console) -> tuple[str, bool]:
    """(пароль, временный ли он). Меню: случайный по умолчанию или свой."""
    console.say(t("menu_title"))
    console.say(t("menu_random"))
    console.say(t("menu_own"))
    while True:
        choice = console.ask(t("menu_choice")).strip() or "1"
        if choice == "1":
            return generate_password(), True
        if choice == "2":
            return read_own_password(console), False
        console.say(t("menu_again"))


def read_own_password(console: Console) -> str:
    password = console.ask_secret(t("password_prompt"))
    if len(password) < MIN_PASSWORD_LENGTH:
        raise CliError(t("password_short", min=MIN_PASSWORD_LENGTH))
    if console.ask_secret(t("password_repeat")) != password:
        raise CliError(t("password_mismatch"))
    return password


async def count_admins() -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Admin)) or 0)


async def create_admin(email: str, password: str, role: AdminRole, *, must_change_password: bool = False) -> None:
    """Создаёт админа и записывает это в аудит. Пароль в аудит не попадает."""
    async with session_factory() as session:
        if await session.scalar(select(Admin.id).where(Admin.email == email)):
            raise CliError(t("exists", email=email))
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
    console.say(t("created_header", email=email))
    console.say("")
    console.say(t("temporary_password", password=password))
    console.say("")
    console.say(t("shown_once"))
    console.say(t("will_ask_new"))


async def run(argv: Sequence[str], console: Console) -> None:
    parser = argparse.ArgumentParser(description=t("description"))
    parser.add_argument("email", nargs="?", help=t("email_help"))
    parser.add_argument("--role", choices=[role.value for role in AdminRole], default=AdminRole.SUPERADMIN.value)
    parser.add_argument("--random-password", action="store_true", help=t("random_help"))
    parser.add_argument("--only-if-empty", action="store_true", help=t("empty_help"))
    args = parser.parse_args(argv)

    if args.only_if_empty and await count_admins() > 0:
        console.say(t("admins_exist"))
        return

    raw_email = args.email
    if raw_email is None:
        if not console.interactive:
            raise CliError(t("email_needed"))
        raw_email = console.ask(t("email_prompt"))
    email = parse_email(raw_email)
    role = AdminRole(args.role)

    # Всё, что может отказать, — до создания: недосозданный админ без показанного пароля хуже отказа.
    if args.random_password:
        password, temporary = generate_password(), True
    elif console.interactive:
        password, temporary = choose_password(console)
    else:
        raise CliError(t("no_terminal_for_password"))
    if temporary and not console.interactive:
        raise CliError(t("generated_needs_terminal"))

    await create_admin(email, password, role, must_change_password=temporary)
    if temporary:
        show_generated_password(console, email, password)
    else:
        console.say(t("created_role", email=email, role=role.value))


def main() -> None:
    try:
        asyncio.run(run(sys.argv[1:], Console.system()))
    except CliError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
