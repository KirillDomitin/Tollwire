#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Приглашения в Telegram-бота из консоли сервера панели (экрана «Бот» пока нет; `./panel bot invite`).

    docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.bot_invite protocols
    docker compose … run --rm api python -m scripts.bot_invite create --protocol <id> --days 30 --expires-in 7d

  protocols                                  протоколы, на которые можно пригласить (id, сервер, тип)
  create --protocol ID --days N --expires-in 7d|12h [--note TEXT] [--raw]
                                             одноразовое приглашение. Ссылка t.me/<бот>?start=<код> показывается один
                                             раз и только в терминале: при выводе в файл или через конвейер скрипт
                                             отказывается ДО создания, чтобы код не остался в логе. --raw печатает одну
                                             ссылку (или один код, если бот ещё не присылал heartbeat и имя неизвестно).

Приглашение, созданное здесь, неотличимо от созданного через админский API; в аудите - source: cli, без кода.
"""

import argparse
import asyncio
import re
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from sqlalchemy import select

from core.database.postgres import session_factory
from core.exceptions import InvalidDataError, NotFoundError
from modules.audit.actor import SYSTEM_ACTOR
from modules.bot.invites import BotInviteService
from modules.bot.schemas import InviteCreate
from modules.protocols.models.protocol import VPNProtocol
from modules.servers.models.server import VPNServer
from scripts.cli_common import CliError, Console, translator

SOURCE = "cli"
_DEADLINE = re.compile(r"(\d+)([dh])")

MESSAGES: dict[str, dict[str, str]] = {
    "description": {"ru": "Приглашения в Telegram-бота", "en": "Telegram bot invitations"},
    "protocols_help": {"ru": "показать протоколы для приглашения", "en": "list the protocols to invite to"},
    "create_help": {"ru": "создать приглашение (показывается один раз)", "en": "create an invitation (shown once)"},
    "protocol_help": {"ru": "id протокола (см. команду protocols)", "en": "protocol id (see the protocols command)"},
    "days_help": {"ru": "на сколько дней выдаётся доступ (1-3650)", "en": "days of access to give (1-3650)"},
    "expires_help": {
        "ru": "сколько действует приглашение: 7d или 12h",
        "en": "how long the invitation is valid: 7d or 12h",
    },
    "note_help": {"ru": "заметка, кому (до 100 символов)", "en": "a note, for whom (up to 100 characters)"},
    "raw_help": {"ru": "напечатать только ссылку", "en": "print the link alone"},
    "needs_terminal": {
        "ru": "Приглашение можно показать только в терминале (не в файле и не через конвейер): иначе код попадёт в "
        "лог. Запустите команду в терминале или добавьте --raw и сохраните вывод сами.",
        "en": "The invitation can only be shown in a terminal (not to a file or a pipe), otherwise the code would end "
        "up in a log. Run the command in a terminal or add --raw and capture the output yourself.",
    },
    "no_protocols": {"ru": "Протоколов нет.", "en": "There are no protocols."},
    "bad_protocol": {"ru": "Некорректный id протокола.", "en": "Invalid protocol id."},
    "protocol_not_found": {"ru": "Такого протокола нет.", "en": "No such protocol."},
    "bad_deadline": {
        "ru": "Срок приглашения: число с единицей, например 7d или 12h, больше нуля.",
        "en": "The invitation deadline is a number with a unit, for example 7d or 12h, above zero.",
    },
    "invalid": {
        "ru": "Недопустимые значения: дней от 1 до 3650, заметка до 100 символов.",
        "en": "Invalid values: 1 to 3650 days, a note of up to 100 characters.",
    },
    "created": {"ru": "Приглашение создано.", "en": "Invitation created."},
    "link_line": {"ru": "    Ссылка:  {link}", "en": "    Link:  {link}"},
    "code_line": {
        "ru": "    Код:  {code}  (бот ещё не выходил на связь: имя бота неизвестно)",
        "en": "    Code:  {code}  (the bot has not reported yet: its name is unknown)",
    },
    "shown_once": {
        "ru": "Она показана один раз и нигде не сохранена: отправьте её человеку сейчас.",
        "en": "It is shown once and stored nowhere: send it to the person now.",
    },
}
t = translator(MESSAGES)


def parse_deadline(raw: str) -> timedelta:
    match = _DEADLINE.fullmatch(raw.strip())
    amount = int(match.group(1)) if match else 0
    if not match or amount <= 0:
        raise CliError(t("bad_deadline"))
    return timedelta(days=amount) if match.group(2) == "d" else timedelta(hours=amount)


def parse_protocol(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise CliError(t("bad_protocol")) from None


async def protocols(console: Console) -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(VPNProtocol, VPNServer.name)
                .join(VPNServer, VPNServer.id == VPNProtocol.server_id)
                .order_by(VPNServer.name, VPNProtocol.type)
            )
        ).all()
    shown = [(protocol, server) for protocol, server in rows if not protocol.type.name.startswith("AMNEZIA_APP")]
    if not shown:
        console.say(t("no_protocols"))
    for protocol, server in shown:
        console.say(f"{protocol.id}  {server}  {protocol.type.value}")


async def create(protocol: str, days: int, expires_in: str, note: str | None, raw: bool, console: Console) -> None:
    if not raw and not console.interactive:
        raise CliError(t("needs_terminal"))  # до создания: созданное и не показанное приглашение хуже отказа
    protocol_id = parse_protocol(protocol)
    deadline = datetime.now(timezone.utc) + parse_deadline(expires_in)
    try:
        data = InviteCreate(protocol_id=protocol_id, days=days, expires_at=deadline, note=note or None)
    except ValidationError:
        raise CliError(t("invalid")) from None
    async with session_factory() as session:
        service = BotInviteService(session, SYSTEM_ACTOR)
        try:
            _, code = await service.create(data, source=SOURCE)
        except NotFoundError:
            raise CliError(t("protocol_not_found")) from None
        except InvalidDataError:
            raise CliError(t("bad_deadline")) from None
        link = await service.link(code)
    if raw:
        console.say(link or code)
        return
    console.say("")
    console.say(t("created"))
    console.say("")
    console.say(t("link_line", link=link) if link else t("code_line", code=code))
    console.say("")
    console.say(t("shown_once"))


async def run(argv: Sequence[str], console: Console) -> int:
    parser = argparse.ArgumentParser(description=t("description"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("protocols", help=t("protocols_help"))
    create_parser = commands.add_parser("create", help=t("create_help"))
    create_parser.add_argument("--protocol", required=True, help=t("protocol_help"))
    create_parser.add_argument("--days", required=True, type=int, help=t("days_help"))
    create_parser.add_argument("--expires-in", required=True, help=t("expires_help"))
    create_parser.add_argument("--note", help=t("note_help"))
    create_parser.add_argument("--raw", action="store_true", help=t("raw_help"))
    args = parser.parse_args(argv)

    if args.command == "protocols":
        await protocols(console)
    else:
        await create(args.protocol, args.days, args.expires_in, args.note, args.raw, console)
    return 0


def main() -> None:
    try:
        code = asyncio.run(run(sys.argv[1:], Console.system()))
    except CliError as error:
        sys.exit(str(error))
    sys.exit(code)


if __name__ == "__main__":
    main()
