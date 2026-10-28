"""Настройки бота (этап 25.4, Р5-A): таблица `bot_settings` с одной строкой и значения по умолчанию для приглашений.

Настоящий Postgres. Тестовая БД чистится TRUNCATE после каждого теста, поэтому строка `id = 1`, которую вставила
миграция, здесь пропадает: сервис обязан создавать её сам при первом обращении."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from core.database.postgres import session_factory
from core.exceptions import NotFoundError, ReasonConflictError
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.auth.models.admin import AdminRole
from modules.bot.invites import BotInviteService
from modules.bot.models.invite import BotInvite
from modules.bot.schemas import InviteCreate
from tests.bot_support import NO_DEFAULT_PROTOCOL, audit_dump, create_invite, now, sql, sql_rows
from tests.fixtures import World
from tests.integration.conftest import make_admin

DEFAULTS = {"default_protocol_id": None, "default_days": 30, "invite_valid_days": 7, "panel_down_after_seconds": 300}


def settings_service(session: Any, actor: AuditActor = SYSTEM_ACTOR) -> Any:
    from modules.bot.settings import BotSettingsService

    return BotSettingsService(session, actor)


def update_data(**fields: Any) -> Any:
    from modules.bot.schemas import BotSettingsUpdate

    return BotSettingsUpdate(**{**{"default_protocol_id": None, "default_days": 30, "invite_valid_days": 7}, **fields})


async def stored_settings() -> list[tuple[Any, ...]]:
    return await sql_rows(
        "SELECT id, default_protocol_id, default_days, invite_valid_days, panel_down_after_seconds FROM bot_settings"
    )


async def settings_events() -> list[AuditLog]:
    async with session_factory() as session:
        query = (
            select(AuditLog).where(AuditLog.action == AuditAction("bot_settings_updated")).order_by(AuditLog.created_at)
        )
        return list((await session.execute(query)).scalars())


async def invite_in_db(invite_id: UUID) -> BotInvite:
    async with session_factory() as session:
        invite = await session.get(BotInvite, invite_id)
    assert invite is not None
    return invite


async def set_defaults(world: World, protocol_id: UUID | None, *, days: int = 14, valid_days: int = 3) -> None:
    await settings_service(world.session).update(
        update_data(default_protocol_id=protocol_id, default_days=days, invite_valid_days=valid_days)
    )


# --- сами настройки ------------------------------------------------------------------------------------------------


async def test_the_first_read_creates_the_single_row_with_the_agreed_defaults(world: World) -> None:
    assert await stored_settings() == []  # таблица очищена после предыдущего теста

    current = await settings_service(world.session).get()

    assert current.default_protocol_id is None and current.default_days == 30
    assert (current.invite_valid_days, current.panel_down_after_seconds) == (7, 300)
    assert await stored_settings() == [(1, None, 30, 7, 300)]


async def test_reading_twice_keeps_one_row(world: World) -> None:
    await settings_service(world.session).get()
    await settings_service(world.session).get()

    assert len(await stored_settings()) == 1


async def test_an_update_is_stored_and_a_new_reader_sees_it(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None

    await settings_service(world.session).update(
        update_data(default_protocol_id=protocol.id, default_days=90, invite_valid_days=14)
    )

    assert await stored_settings() == [(1, protocol.id, 90, 14, 300)]
    async with session_factory() as other:
        seen = await settings_service(other).get()
    assert (seen.default_protocol_id, seen.default_days, seen.invite_valid_days) == (protocol.id, 90, 14)


async def test_an_update_remembers_who_made_it_and_when(world: World) -> None:
    login = await make_admin(world.session, AdminRole.OPERATOR)
    before = datetime.now(timezone.utc)

    changed = await settings_service(world.session, AuditActor(admin_id=login.admin.id)).update(
        update_data(default_days=10)
    )

    assert changed.updated_by == login.admin.id
    assert changed.updated_at >= before - timedelta(seconds=1)


async def test_an_update_does_not_touch_the_threshold_the_ui_does_not_show_yet(world: World) -> None:
    await settings_service(world.session).update(update_data(default_days=10))

    assert (await stored_settings())[0][4] == DEFAULTS["panel_down_after_seconds"]


async def test_the_default_protocol_must_exist(world: World) -> None:
    with pytest.raises(NotFoundError):
        await settings_service(world.session).update(update_data(default_protocol_id=uuid4()))


async def test_the_default_protocol_may_be_cleared(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    await set_defaults(world, protocol.id)

    await settings_service(world.session).update(update_data(default_protocol_id=None))

    assert (await stored_settings())[0][1] is None


async def test_deleting_the_default_protocol_clears_the_default(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    await set_defaults(world, protocol.id)

    await sql("DELETE FROM protocols WHERE id = :id", id=protocol.id)

    assert (await stored_settings())[0][1] is None


# --- аудит настроек ------------------------------------------------------------------------------------------------


async def test_the_audit_of_an_update_lists_only_what_changed_as_was_and_became(world: World) -> None:
    await settings_service(world.session).update(update_data(default_days=30, invite_valid_days=7))  # создали строку

    await settings_service(world.session).update(update_data(default_days=90, invite_valid_days=7))

    event = (await settings_events())[-1]
    assert event.entity_type == "bot_settings"
    assert event.meta == {"changes": {"default_days": [30, 90]}}


async def test_the_audit_of_a_protocol_change_names_ids_not_servers(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None

    await settings_service(world.session).update(update_data(default_protocol_id=protocol.id))

    event = (await settings_events())[-1]
    assert event.meta["changes"]["default_protocol_id"] == [None, str(protocol.id)]


# --- значения по умолчанию в приглашении ---------------------------------------------------------------------------


async def test_an_invite_without_details_takes_protocol_days_and_deadline_from_the_settings(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    await set_defaults(world, protocol.id, days=14, valid_days=3)
    started = now()

    invite, code = await BotInviteService(world.session, SYSTEM_ACTOR).create(InviteCreate())

    stored = await invite_in_db(invite.id)
    assert stored.protocol_id == protocol.id and stored.days == 14
    assert abs((stored.expires_at - (started + timedelta(days=3))).total_seconds()) < 120
    assert code


async def test_what_the_request_gives_wins_over_the_settings(world: World) -> None:
    _, default, _ = await world.server()
    _, other, _ = await world.server()
    assert default is not None and other is not None
    await set_defaults(world, default.id, days=14, valid_days=3)
    deadline = now() + timedelta(days=20)

    invite, _ = await BotInviteService(world.session, SYSTEM_ACTOR).create(
        InviteCreate(protocol_id=other.id, days=5, expires_at=deadline)
    )

    stored = await invite_in_db(invite.id)
    assert (stored.protocol_id, stored.days) == (other.id, 5)
    assert abs((stored.expires_at - deadline).total_seconds()) < 1


async def test_only_the_missing_details_come_from_the_settings(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    await set_defaults(world, protocol.id, days=14, valid_days=3)

    invite, _ = await BotInviteService(world.session, SYSTEM_ACTOR).create(InviteCreate(days=2))

    stored = await invite_in_db(invite.id)
    assert stored.protocol_id == protocol.id and stored.days == 2


async def test_without_a_protocol_in_the_request_and_in_the_settings_the_invite_is_refused(world: World) -> None:
    with pytest.raises(ReasonConflictError) as caught:
        await BotInviteService(world.session, SYSTEM_ACTOR).create(InviteCreate(days=5))

    assert caught.value.reason == NO_DEFAULT_PROTOCOL
    assert await sql_rows("SELECT id FROM bot_invites") == []


async def test_a_protocol_in_the_request_makes_the_missing_default_irrelevant(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None

    invite, _ = await BotInviteService(world.session, SYSTEM_ACTOR).create(InviteCreate(protocol_id=protocol.id))

    stored = await invite_in_db(invite.id)
    assert stored.protocol_id == protocol.id and stored.days == 30  # дни - по умолчанию из настроек (30)


async def test_the_default_protocol_that_was_deleted_leaves_no_default(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    await set_defaults(world, protocol.id)
    await sql("DELETE FROM protocols WHERE id = :id", id=protocol.id)

    with pytest.raises(ReasonConflictError) as caught:
        await BotInviteService(world.session, SYSTEM_ACTOR).create(InviteCreate())

    assert caught.value.reason == NO_DEFAULT_PROTOCOL


async def test_an_invite_with_defaults_is_audited_with_the_values_it_really_got(world: World) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    await set_defaults(world, protocol.id, days=14)

    invite, code = await BotInviteService(world.session, SYSTEM_ACTOR).create(InviteCreate())

    async with session_factory() as session:
        query = select(AuditLog).where(AuditLog.action == AuditAction.BOT_INVITE_CREATED)
        (event,) = (await session.execute(query)).scalars().all()
    assert event.entity_id == invite.id
    assert event.meta["days"] == 14 and event.meta["protocol_id"] == str(protocol.id)
    assert code not in await audit_dump()


async def test_the_explicit_helper_of_the_older_tests_still_works(world: World) -> None:
    """`create_invite` из 25.1 передаёт всё явно: настройки ему не нужны."""
    _, protocol, _ = await world.server()
    assert protocol is not None

    invite, _ = await create_invite(world.session, protocol.id, days=11)

    assert (await invite_in_db(invite.id)).days == 11
