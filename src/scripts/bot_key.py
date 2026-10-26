#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Ключи Telegram-бота из консоли сервера панели (API ключей требует JWT superadmin, а в install.sh и ./panel его нет).

  docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm -T api python -m scripts.bot_key …

create --name N [--raw]   выпустить ключ. Показывается один раз и только в терминале: при выводе в файл или через
                          конвейер (`| tee install.log`) скрипт отказывается ДО выпуска, чтобы ключ не остался в
                          логе. --raw печатает один только ключ (для `key=$(… --raw)` в скриптах установки).
revoke                    отозвать ключ; сам ключ читается из stdin (аргументы видны всем в `ps`)
list                      ключи: префикс, имя, состояние, когда бот был на связи (самих ключей нет, только префикс)
seen --within S           код выхода 0, если бот с этим ключом (stdin) выходил на связь не позже S секунд назад
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import datetime, timezone

from pydantic import ValidationError

from core.database.postgres import session_factory
from core.security import hash_token
from modules.audit.actor import SYSTEM_ACTOR
from modules.bot.keys import BotKeyService
from modules.bot.models.key import BotKey
from modules.bot.repository import BotKeyRepository
from modules.bot.schemas import BotKeyCreate
from scripts.cli_common import CliError, Console, translator

SOURCE = "cli"  # метка в аудите: ключ выпущен консолью, а не админом через API

MESSAGES: dict[str, dict[str, str]] = {
    "description": {"ru": "Ключи Telegram-бота", "en": "Telegram bot keys"},
    "create_help": {"ru": "выпустить ключ (показывается один раз)", "en": "issue a key (shown once)"},
    "revoke_help": {"ru": "отозвать ключ (читается из stdin)", "en": "revoke a key (read from stdin)"},
    "list_help": {"ru": "показать ключи", "en": "list the keys"},
    "seen_help": {
        "ru": "бот выходил на связь недавно? (ключ из stdin)",
        "en": "was the bot seen lately? (key on stdin)",
    },
    "name_help": {"ru": "имя ключа, например название бота", "en": "key name, for example the bot name"},
    "raw_help": {"ru": "напечатать только ключ", "en": "print the key alone"},
    "within_help": {"ru": "окно в секундах", "en": "window in seconds"},
    "needs_terminal": {
        "ru": "Ключ можно показать только в терминале (не в файле и не через конвейер): иначе он попадёт в лог. "
        "Запустите команду в терминале или добавьте --raw и сохраните вывод сами.",
        "en": "The key can only be shown in a terminal (not to a file or a pipe), otherwise it would end up in a "
        "log. Run the command in a terminal or add --raw and capture the output yourself.",
    },
    "invalid_name": {"ru": "Имя ключа: от 1 до 64 символов.", "en": "The key name must be 1 to 64 characters."},
    "created": {"ru": "Ключ бота «{name}» выпущен.", "en": "Bot key '{name}' issued."},
    "key_line": {"ru": "    Ключ:  {key}", "en": "    Key:  {key}"},
    "shown_once": {
        "ru": "Он показан один раз и нигде не сохранён: сохраните его сейчас.",
        "en": "It is shown once and stored nowhere: save it now.",
    },
    "key_not_found": {"ru": "Такого ключа нет.", "en": "No such key."},
    "revoked": {"ru": "Ключ «{name}» отозван.", "en": "Key '{name}' revoked."},
    "nothing": {"ru": "Ключей бота нет.", "en": "There are no bot keys."},
    "state_active": {"ru": "действует", "en": "active"},
    "state_revoked": {"ru": "отозван", "en": "revoked"},
    "never_seen": {"ru": "на связи не был", "en": "never seen"},
    "seen_at": {"ru": "на связи {at}", "en": "seen {at}"},
    "not_seen": {"ru": "Бот на связи не был.", "en": "The bot has not been seen."},
    "seen_ok": {"ru": "Бот на связи.", "en": "The bot is seen."},
    "bad_window": {"ru": "Окно: целое число секунд больше нуля.", "en": "The window is a whole number of seconds > 0."},
}
t = translator(MESSAGES)


def positive_seconds(raw: str) -> int:
    if not raw.isdigit() or int(raw) <= 0:
        raise argparse.ArgumentTypeError(t("bad_window"))
    return int(raw)


def read_key(console: Console) -> str:
    return console.read_stdin().strip()


async def find_by_secret(secret: str, *, active_only: bool) -> BotKey | None:
    if not secret:
        return None
    async with session_factory() as session:
        keys = BotKeyRepository(session)
        return await (keys.get_active_by_hash if active_only else keys.get_by_hash)(hash_token(secret))


async def create(name: str, raw: bool, console: Console) -> None:
    if not raw and not console.interactive:
        raise CliError(t("needs_terminal"))  # до выпуска: выпущенный и не показанный ключ хуже отказа
    try:
        data = BotKeyCreate(name=name)
    except ValidationError:
        raise CliError(t("invalid_name")) from None
    async with session_factory() as session:
        key, secret = await BotKeyService(session, SYSTEM_ACTOR).create(data, source=SOURCE)
    if raw:
        console.say(secret)
        return
    console.say("")
    console.say(t("created", name=key.name))
    console.say("")
    console.say(t("key_line", key=secret))
    console.say("")
    console.say(t("shown_once"))


async def revoke(console: Console) -> None:
    key = await find_by_secret(read_key(console), active_only=False)
    if key is None:
        raise CliError(t("key_not_found"))
    async with session_factory() as session:
        await BotKeyService(session, SYSTEM_ACTOR).revoke(key.id, source=SOURCE)  # повторный отзыв — не ошибка
    console.say(t("revoked", name=key.name))


async def list_keys(console: Console) -> None:
    async with session_factory() as session:
        keys = await BotKeyService(session, SYSTEM_ACTOR).list()
    if not keys:
        console.say(t("nothing"))
    for key in keys:
        state = t("state_revoked") if key.revoked_at else t("state_active")
        seen = (
            t("seen_at", at=key.last_seen_at.strftime("%Y-%m-%d %H:%M:%S UTC")) if key.last_seen_at else t("never_seen")
        )
        details = " ".join(
            part for part in (key.last_seen_version, f"@{key.bot_username}" if key.bot_username else "") if part
        )
        console.say("  ".join(part for part in (f"{key.key_prefix}…", key.name, state, seen, details) if part))


async def seen(within: int, console: Console) -> int:
    key = await find_by_secret(read_key(console), active_only=True)
    now = datetime.now(timezone.utc)
    if key is None or key.last_seen_at is None or (now - key.last_seen_at).total_seconds() > within:
        console.say(t("not_seen"))
        return 1
    console.say(t("seen_ok"))
    return 0


async def run(argv: Sequence[str], console: Console) -> int:
    parser = argparse.ArgumentParser(description=t("description"))
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create", help=t("create_help"))
    create_parser.add_argument("--name", required=True, help=t("name_help"))
    create_parser.add_argument("--raw", action="store_true", help=t("raw_help"))
    commands.add_parser("revoke", help=t("revoke_help"))
    commands.add_parser("list", help=t("list_help"))
    seen_parser = commands.add_parser("seen", help=t("seen_help"))
    seen_parser.add_argument("--within", type=positive_seconds, required=True, help=t("within_help"))
    args = parser.parse_args(argv)

    if args.command == "create":
        await create(args.name, args.raw, console)
    elif args.command == "revoke":
        await revoke(console)
    elif args.command == "list":
        await list_keys(console)
    else:
        return await seen(args.within, console)
    return 0


def main() -> None:
    try:
        code = asyncio.run(run(sys.argv[1:], Console.system()))
    except CliError as error:
        sys.exit(str(error))
    sys.exit(code)


if __name__ == "__main__":
    main()
