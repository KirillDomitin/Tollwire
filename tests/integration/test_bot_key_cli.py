"""Консольный скрипт ключей бота `scripts.bot_key` (этап 25.2): create / revoke / list / seen.

Скрипт запускают установщик и `./panel bot` через `compose run --rm -T api python -m scripts.bot_key …`. Главное, что
ловят тесты: ключ виден один раз и только человеку в терминале (или явным `--raw`), в БД лежит только хеш, в аудите
`source: cli` без ключа, `revoke` и `seen` берут ключ из stdin, а не из аргументов (аргументы видны в `ps`)."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import hash_token
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.bot.models.key import BotKey
from scripts import bot_key
from tests.bot_cli_support import KEY, keys_in, run_cli
from tests.bot_support import audit_dump, bot_client, create_key

HEARTBEAT = {"bot_username": "tollwire_cli_bot", "version": "0.1.0"}


@pytest.fixture(autouse=True)
def russian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANEL_LANG", "ru")


async def stored(session: AsyncSession) -> list[BotKey]:
    session.expire_all()
    return list((await session.execute(select(BotKey).order_by(BotKey.created_at))).scalars())


async def audit_entries(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    session.expire_all()
    return list((await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


# --- create ----------------------------------------------------------------------------------------------------------


async def test_create_in_a_terminal_shows_the_key_once_with_a_warning(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await run_cli(bot_key, "create", "--name", "home-bot", monkeypatch=monkeypatch)

    assert result.ok, result.error
    (secret,) = keys_in(result.screen)  # ровно одно вхождение: ключ напечатан один раз
    assert "один раз" in result.screen  # сказано, что потом его не увидеть


async def test_create_keeps_only_the_hash_and_the_prefix(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await run_cli(bot_key, "create", "--name", "home-bot", monkeypatch=monkeypatch)

    (secret,) = keys_in(result.screen)
    (row,) = await stored(session)
    assert row.name == "home-bot" and row.revoked_at is None
    assert row.key_hash == hash_token(secret) and row.key_prefix == secret[:8]
    assert secret not in str(row.__dict__)
    assert row.created_by is None  # выпустила консоль, а не админ панели


async def test_create_is_audited_as_cli_without_the_key(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_cli(bot_key, "create", "--name", "home-bot", monkeypatch=monkeypatch)

    (secret,) = keys_in(result.screen)
    (entry,) = await audit_entries(session, AuditAction.BOT_KEY_CREATED)
    assert entry.admin_id is None
    assert entry.meta == {"name": "home-bot", "source": "cli"}
    assert secret not in await audit_dump() and hash_token(secret) not in await audit_dump()


async def test_the_issued_key_opens_the_bot_api(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_cli(bot_key, "create", "--name", "home-bot", monkeypatch=monkeypatch)

    (secret,) = keys_in(result.screen)
    async with bot_client(secret) as bot:
        assert (await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 200


async def test_create_without_a_terminal_is_refused_and_leaves_no_key(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вывод в файл или конвейер (`| tee install.log`, CI) - ключ осел бы в логе, поэтому отказ ДО выпуска."""
    result = await run_cli(bot_key, "create", "--name", "home-bot", monkeypatch=monkeypatch, interactive=False)

    assert not result.ok and "--raw" in result.error  # сказано, как получить ключ осознанно
    assert keys_in(result.everything) == []
    assert await stored(session) == []
    assert await audit_entries(session, AuditAction.BOT_KEY_CREATED) == []


async def test_raw_prints_the_key_alone_for_a_script_to_capture(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await run_cli(bot_key, "create", "--name", "home-bot", "--raw", monkeypatch=monkeypatch, interactive=False)

    assert result.ok, result.error
    (line,) = result.said  # `key=$(… --raw)` получает только ключ: ни рамки, ни пояснений
    assert KEY.fullmatch(line.strip())
    (row,) = await stored(session)
    assert row.key_hash == hash_token(line.strip())


async def test_raw_in_a_terminal_prints_the_key_alone_too(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_cli(bot_key, "create", "--name", "home-bot", "--raw", monkeypatch=monkeypatch)

    (line,) = result.said
    assert KEY.fullmatch(line.strip())


async def test_raw_key_is_audited_without_the_key(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_cli(bot_key, "create", "--name", "home-bot", "--raw", monkeypatch=monkeypatch, interactive=False)

    (entry,) = await audit_entries(session, AuditAction.BOT_KEY_CREATED)
    assert entry.meta == {"name": "home-bot", "source": "cli"}
    assert result.said[0].strip() not in await audit_dump()


async def test_every_call_issues_a_different_key(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    first = await run_cli(bot_key, "create", "--name", "a", "--raw", monkeypatch=monkeypatch)
    second = await run_cli(bot_key, "create", "--name", "b", "--raw", monkeypatch=monkeypatch)

    assert first.said != second.said and len(await stored(session)) == 2


@pytest.mark.parametrize(
    "arguments",
    [["create"], ["create", "--name", ""], ["create", "--name", "   "], ["create", "--name", "x" * 65]],
    ids=["no name", "empty name", "blank name", "65 characters"],
)
async def test_create_needs_a_sensible_name(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    result = await run_cli(bot_key, *arguments, monkeypatch=monkeypatch)

    assert not result.ok
    assert keys_in(result.everything) == [] and await stored(session) == []


async def test_the_name_is_trimmed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await run_cli(bot_key, "create", "--name", "  home-bot  ", monkeypatch=monkeypatch)

    (row,) = await stored(session)
    assert row.name == "home-bot"


async def test_unknown_command_creates_nothing(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_cli(bot_key, "frobnicate", monkeypatch=monkeypatch)

    assert not result.ok and await stored(session) == []


# --- revoke ----------------------------------------------------------------------------------------------------------


async def test_revoke_reads_the_key_from_stdin_and_disables_it(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, secret = await create_key(session, "old-bot")

    result = await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=secret + "\n", interactive=False)

    assert result.ok, result.error
    (row,) = await stored(session)
    assert row.revoked_at is not None
    async with bot_client(secret) as bot:
        assert (await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 401
    assert secret not in result.everything  # ключ в ответ не эхом


async def test_revoke_does_not_take_the_key_as_an_argument(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Аргумент командной строки виден всем в `ps`: ключ принимается только через stdin."""
    key, secret = await create_key(session, "old-bot")

    result = await run_cli(bot_key, "revoke", secret, monkeypatch=monkeypatch, interactive=False)

    assert not result.ok
    (row,) = await stored(session)
    assert row.revoked_at is None


async def test_revoke_touches_only_the_given_key(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _, old = await create_key(session, "old-bot")
    other, other_secret = await create_key(session, "other-bot")

    await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=old, interactive=False)

    rows = {row.name: row for row in await stored(session)}
    assert rows["old-bot"].revoked_at is not None and rows["other-bot"].revoked_at is None
    async with bot_client(other_secret) as bot:
        assert (await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 200


async def test_revoke_is_audited_as_cli_without_the_key(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _, secret = await create_key(session, "old-bot")

    await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=secret, interactive=False)

    (entry,) = await audit_entries(session, AuditAction.BOT_KEY_REVOKED)
    assert entry.admin_id is None and entry.meta == {"name": "old-bot", "source": "cli"}
    assert secret not in await audit_dump()


async def test_revoking_twice_is_harmless_and_audited_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, secret = await create_key(session, "old-bot")

    first = await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=secret, interactive=False)
    second = await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=secret, interactive=False)

    assert first.ok and second.ok
    assert len(await audit_entries(session, AuditAction.BOT_KEY_REVOKED)) == 1


@pytest.mark.parametrize(
    "stdin",
    ["", "\n", "not-a-key", "twb_" + "A" * 43, "twb_short"],
    ids=["empty", "newline", "garbage", "unknown", "short"],
)
async def test_revoke_of_a_missing_key_fails_and_changes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    await create_key(session, "bystander")

    result = await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=stdin, interactive=False)

    assert not result.ok
    assert all(row.revoked_at is None for row in await stored(session))
    assert await audit_entries(session, AuditAction.BOT_KEY_REVOKED) == []


async def test_revoke_survives_whitespace_around_the_key(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, secret = await create_key(session, "old-bot")

    result = await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=f"  {secret}\r\n", interactive=False)

    assert result.ok and (await stored(session))[0].revoked_at is not None


# --- seen ------------------------------------------------------------------------------------------------------------


async def beat(session: AsyncSession, key: BotKey, seconds_ago: float) -> None:
    key.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    await session.commit()


@pytest.mark.parametrize(
    ("ago", "within", "expected"), [(5, 90, True), (60, 90, True), (200, 90, False), (60, 30, False)]
)
async def test_seen_is_ok_when_the_heartbeat_is_fresh_enough(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, ago: int, within: int, expected: bool
) -> None:
    key, secret = await create_key(session, "bot")
    await beat(session, key, ago)

    result = await run_cli(
        bot_key, "seen", "--within", str(within), monkeypatch=monkeypatch, stdin=secret, interactive=False
    )

    assert result.ok is expected


async def test_seen_is_not_ok_for_a_bot_that_never_called(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, secret = await create_key(session, "bot")

    result = await run_cli(bot_key, "seen", "--within", "90", monkeypatch=monkeypatch, stdin=secret, interactive=False)

    assert not result.ok


async def test_a_real_heartbeat_makes_seen_ok(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _, secret = await create_key(session, "bot")
    async with bot_client(secret) as bot:
        assert (await bot.post("/api/v1/bot/heartbeat", json=HEARTBEAT)).status_code == 200

    result = await run_cli(bot_key, "seen", "--within", "90", monkeypatch=monkeypatch, stdin=secret, interactive=False)

    assert result.ok


async def test_seen_ignores_the_heartbeats_of_other_keys(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Новый ключ при `./panel bot key`: старый бот ещё жив, но проверяется именно новый."""
    old, _ = await create_key(session, "old")
    await beat(session, old, 1)
    _, new_secret = await create_key(session, "new")

    result = await run_cli(
        bot_key, "seen", "--within", "90", monkeypatch=monkeypatch, stdin=new_secret, interactive=False
    )

    assert not result.ok


async def test_seen_is_not_ok_for_a_revoked_key_even_with_a_fresh_heartbeat(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, secret = await create_key(session, "bot")
    await beat(session, key, 1)
    await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=secret, interactive=False)

    result = await run_cli(bot_key, "seen", "--within", "90", monkeypatch=monkeypatch, stdin=secret, interactive=False)

    assert not result.ok


@pytest.mark.parametrize("stdin", ["", "garbage", "twb_" + "B" * 43])
async def test_seen_is_not_ok_for_a_missing_key(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    key, _ = await create_key(session, "bot")
    await beat(session, key, 1)

    result = await run_cli(bot_key, "seen", "--within", "90", monkeypatch=monkeypatch, stdin=stdin, interactive=False)

    assert not result.ok


@pytest.mark.parametrize("within", ["abc", "-5", "1.5", ""])
async def test_seen_needs_a_sensible_window(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, within: str
) -> None:
    key, secret = await create_key(session, "bot")
    await beat(session, key, 1)

    result = await run_cli(
        bot_key, "seen", "--within", within, monkeypatch=monkeypatch, stdin=secret, interactive=False
    )

    assert not result.ok


async def test_seen_does_not_change_anything_or_print_the_key(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, secret = await create_key(session, "bot")
    await beat(session, key, 5)
    before = (await stored(session))[0].last_seen_at

    result = await run_cli(bot_key, "seen", "--within", "90", monkeypatch=monkeypatch, stdin=secret, interactive=False)

    assert secret not in result.everything
    assert (await stored(session))[0].last_seen_at == before  # проверка не считается «опросом» бота


# --- list ------------------------------------------------------------------------------------------------------------


async def test_list_shows_names_prefixes_and_revoked_marks_but_never_a_key(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, live_secret = await create_key(session, "live-bot")
    dead, dead_secret = await create_key(session, "dead-bot")
    await run_cli(bot_key, "revoke", monkeypatch=monkeypatch, stdin=dead_secret, interactive=False)

    result = await run_cli(bot_key, "list", monkeypatch=monkeypatch, interactive=False)

    assert result.ok, result.error
    (live_line,) = [line for line in result.said if "live-bot" in line]
    (dead_line,) = [line for line in result.said if "dead-bot" in line]
    assert live_secret[:8] in live_line and dead_secret[:8] in dead_line
    assert "отозван" in dead_line and "отозван" not in live_line
    assert keys_in(result.everything) == []  # полного значения нет, только префиксы


async def test_list_of_nothing_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_cli(bot_key, "list", monkeypatch=monkeypatch, interactive=False)

    assert result.ok and keys_in(result.everything) == []


async def test_list_shows_when_the_bot_was_last_seen(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    key, _ = await create_key(session, "seen-bot")
    key.last_seen_at, key.last_seen_version, key.bot_username = (
        datetime.now(timezone.utc),
        "0.1.0",
        "tollwire_cli_bot",
    )
    await session.commit()

    result = await run_cli(bot_key, "list", monkeypatch=monkeypatch, interactive=False)

    (line,) = [line for line in result.said if "seen-bot" in line]
    assert "0.1.0" in line and "tollwire_cli_bot" in line


# --- язык ------------------------------------------------------------------------------------------------------------


async def test_messages_are_english_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PANEL_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)

    shown = await run_cli(bot_key, "create", "--name", "home-bot", monkeypatch=monkeypatch)
    refused = await run_cli(bot_key, "create", "--name", "other", monkeypatch=monkeypatch, interactive=False)

    assert shown.ok and not re.search("[А-Яа-я]", shown.screen)
    assert not refused.ok and not re.search("[А-Яа-я]", refused.error) and "--raw" in refused.error


async def test_messages_follow_panel_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANEL_LANG", "ru")

    result = await run_cli(bot_key, "create", "--name", "home-bot", monkeypatch=monkeypatch)

    assert re.search("[А-Яа-я]", result.screen)
