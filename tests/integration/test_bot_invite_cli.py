"""Консольный скрипт приглашений бота `scripts.bot_invite` (этап 25.2): protocols / create.

`./panel bot invite` печатает ссылку `t.me/<бот>?start=<код>`. Как и ключ бота, код видно один раз и только человеку в
терминале (или явным `--raw`); в БД лежит хеш, в аудите - `source: cli` без кода и ссылки. Приглашение, выпущенное
скриптом, неотличимо от созданного через админский API."""

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import hash_token
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.bot.models.invite import BotInvite
from scripts import bot_invite
from tests.bot_cli_support import BOT_NAME, run_cli
from tests.bot_support import BotScene, audit_dump, bot_client, bot_scene
from tests.fixtures import World

CODE_SHAPE = re.compile(r"[A-Za-z0-9_-]{24}")


@pytest.fixture(autouse=True)
def russian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANEL_LANG", "ru")


@pytest.fixture
async def scene(world: World) -> BotScene:
    return await bot_scene(world)


async def bot_is_known(scene: BotScene) -> None:
    """Бот хоть раз присылал heartbeat - панель знает его имя и может собрать ссылку."""
    async with bot_client(scene.key_secret) as bot:
        response = await bot.post("/api/v1/bot/heartbeat", json={"bot_username": BOT_NAME, "version": "0.1.0"})
    assert response.status_code == 200


async def invites(session: AsyncSession) -> list[BotInvite]:
    session.expire_all()
    return list((await session.execute(select(BotInvite).order_by(BotInvite.created_at))).scalars())


def code_in(text: str, invite: BotInvite) -> str:
    """Код приглашения из напечатанного: тот кусок вида `[A-Za-z0-9_-]{24}`, чей хеш лежит в БД."""
    found = [part for part in CODE_SHAPE.findall(text) if hash_token(part) == invite.code_hash]
    assert len(found) >= 1, "the screen does not hold the invitation code"
    return found[0]


def create_args(scene: BotScene, *extra: str) -> tuple[str, ...]:
    return ("create", "--protocol", str(scene.protocol.id), "--days", "30", "--expires-in", "7d", *extra)


# --- protocols -------------------------------------------------------------------------------------------------------


async def test_protocols_lists_what_an_invitation_can_give(scene: BotScene, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_cli(bot_invite, "protocols", monkeypatch=monkeypatch, interactive=False)

    assert result.ok, result.error
    (line,) = [line for line in result.said if str(scene.protocol.id) in line]
    assert scene.server.name in line  # по имени сервера видно, какой протокол выбрать


# --- create ----------------------------------------------------------------------------------------------------------


async def test_create_prints_the_telegram_link_once(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bot_is_known(scene)

    result = await run_cli(bot_invite, *create_args(scene), monkeypatch=monkeypatch)

    assert result.ok, result.error
    (invite,) = await invites(session)
    code = code_in(result.screen, invite)
    assert result.screen.count(f"https://t.me/{BOT_NAME}?start={code}") == 1
    assert result.screen.count(code) == 1  # код встречается один раз: только внутри ссылки


async def test_create_stores_protocol_days_deadline_and_note_and_only_the_hash(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bot_is_known(scene)
    protocol_id = scene.protocol.id  # до expire_all() в invites(): потом атрибуты ORM-объекта придётся перечитывать
    started = datetime.now(timezone.utc)

    result = await run_cli(
        bot_invite, *create_args(scene, "--note", "  Маша из соседнего двора  "), monkeypatch=monkeypatch
    )

    (invite,) = await invites(session)
    assert invite.protocol_id == protocol_id and invite.days == 30
    assert invite.note == "Маша из соседнего двора"
    assert abs((invite.expires_at - (started + timedelta(days=7))).total_seconds()) < 120
    assert invite.used_at is None and invite.revoked_at is None and invite.created_by is None
    assert invite.code_hash == hash_token(code_in(result.screen, invite))


async def test_hours_are_accepted_for_the_deadline(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = datetime.now(timezone.utc)

    result = await run_cli(
        bot_invite,
        "create",
        "--protocol",
        str(scene.protocol.id),
        "--days",
        "5",
        "--expires-in",
        "12h",
        monkeypatch=monkeypatch,
    )

    assert result.ok, result.error
    (invite,) = await invites(session)
    assert abs((invite.expires_at - (started + timedelta(hours=12))).total_seconds()) < 120


async def test_the_invitation_made_by_the_script_works_in_the_bot_api(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await run_cli(bot_invite, *create_args(scene), monkeypatch=monkeypatch)

    (invite,) = await invites(session)
    code = code_in(result.screen, invite)
    async with bot_client(scene.key_secret) as bot:
        checked = await bot.post("/api/v1/bot/invites/check", json={"code": code})
    assert checked.status_code == 200 and checked.json() == {"status": "valid"}


async def test_without_the_bot_name_the_code_is_shown_instead_of_a_broken_link(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Бот ещё не присылал heartbeat - имени нет, `t.me/None?start=…` печатать нельзя."""
    result = await run_cli(bot_invite, *create_args(scene), monkeypatch=monkeypatch)

    assert result.ok, result.error
    (invite,) = await invites(session)
    code_in(result.screen, invite)
    assert "t.me/None" not in result.screen and "None" not in result.screen and "null" not in result.screen


async def test_create_is_audited_as_cli_without_the_code(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bot_is_known(scene)
    protocol_id = scene.protocol.id

    result = await run_cli(bot_invite, *create_args(scene, "--note", "Маша"), monkeypatch=monkeypatch)

    (invite,) = await invites(session)
    code = code_in(result.screen, invite)
    session.expire_all()
    (entry,) = (
        await session.execute(select(AuditLog).where(AuditLog.action == AuditAction.BOT_INVITE_CREATED))
    ).scalars()
    assert entry.admin_id is None and entry.meta["source"] == "cli"
    assert entry.meta["days"] == 30 and str(entry.meta["protocol_id"]) == str(protocol_id)
    dump = await audit_dump()
    assert code not in dump and hash_token(code) not in dump and "t.me" not in dump


# --- без терминала ---------------------------------------------------------------------------------------------------


async def test_create_without_a_terminal_is_refused_and_leaves_no_invitation(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bot_is_known(scene)

    result = await run_cli(bot_invite, *create_args(scene), monkeypatch=monkeypatch, interactive=False)

    assert not result.ok and "--raw" in result.error
    assert CODE_SHAPE.search(result.everything.replace(str(scene.protocol.id), "")) is None
    assert await invites(session) == []


async def test_raw_prints_the_link_alone_for_a_script_to_capture(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bot_is_known(scene)

    result = await run_cli(bot_invite, *create_args(scene, "--raw"), monkeypatch=monkeypatch, interactive=False)

    assert result.ok, result.error
    (line,) = result.said
    (invite,) = await invites(session)
    assert line.strip() == f"https://t.me/{BOT_NAME}?start={code_in(line, invite)}"


# --- ввод ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("days", ["0", "-1", "3651", "abc", "1.5", ""])
async def test_days_must_be_between_1_and_3650(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch, days: str
) -> None:
    result = await run_cli(
        bot_invite,
        "create",
        "--protocol",
        str(scene.protocol.id),
        "--days",
        days,
        "--expires-in",
        "7d",
        monkeypatch=monkeypatch,
    )

    assert not result.ok and await invites(session) == []


@pytest.mark.parametrize("deadline", ["0d", "0h", "-1d", "abc", "d", ""])
async def test_the_deadline_must_be_in_the_future(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch, deadline: str
) -> None:
    result = await run_cli(
        bot_invite,
        "create",
        "--protocol",
        str(scene.protocol.id),
        "--days",
        "30",
        "--expires-in",
        deadline,
        monkeypatch=monkeypatch,
    )

    assert not result.ok and await invites(session) == []


@pytest.mark.parametrize("protocol", ["not-a-uuid", str(uuid.UUID(int=7)), ""], ids=["garbage", "unknown", "empty"])
async def test_the_protocol_must_exist(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, protocol: str) -> None:
    result = await run_cli(
        bot_invite,
        "create",
        "--protocol",
        protocol,
        "--days",
        "30",
        "--expires-in",
        "7d",
        monkeypatch=monkeypatch,
    )

    assert not result.ok and await invites(session) == []


async def test_a_long_note_is_refused(session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_cli(bot_invite, *create_args(scene, "--note", "x" * 101), monkeypatch=monkeypatch)

    assert not result.ok and await invites(session) == []


@pytest.mark.parametrize("missing", ["--protocol", "--days", "--expires-in"])
async def test_required_options_cannot_be_skipped(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    arguments = list(create_args(scene))
    index = arguments.index(missing)
    del arguments[index : index + 2]

    result = await run_cli(bot_invite, *arguments, monkeypatch=monkeypatch)

    assert not result.ok and await invites(session) == []


async def test_every_call_makes_a_new_invitation_with_a_different_code(
    session: AsyncSession, scene: BotScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await run_cli(bot_invite, *create_args(scene), monkeypatch=monkeypatch)
    second = await run_cli(bot_invite, *create_args(scene), monkeypatch=monkeypatch)

    one, two = await invites(session)
    assert code_in(first.screen, one) != code_in(second.screen, two)


async def test_english_output_has_no_russian(scene: BotScene, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PANEL_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)

    shown = await run_cli(bot_invite, *create_args(scene), monkeypatch=monkeypatch)
    refused = await run_cli(bot_invite, *create_args(scene), monkeypatch=monkeypatch, interactive=False)

    assert shown.ok and not re.search("[А-Яа-я]", shown.screen)
    assert not refused.ok and not re.search("[А-Яа-я]", refused.error)
