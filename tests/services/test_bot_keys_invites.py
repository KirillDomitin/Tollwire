"""Ключи бота и приглашения (этап 25.1): секреты только хешем, отзыв идемпотентен, статусы, аудит без секретов."""

import re
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from modules.bot.invites import BotInviteService
from modules.bot.keys import BotKeyService
from modules.bot.schemas import BotKeyCreate
from pydantic import ValidationError
from sqlalchemy import select

from core.database.postgres import session_factory
from core.exceptions import InvalidDataError, NotFoundError
from core.log import redact
from core.security import hash_token
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditLog
from tests.bot_support import (
    EXPIRED,
    REVOKED,
    UNKNOWN,
    USED,
    VALID,
    audit_dump,
    bot_action,
    bot_scene,
    check_invite,
    create_invite,
    create_key,
    now,
    register,
)
from tests.fixtures import World
from tests.integration.conftest import make_admin


def column_values(row: Any) -> str:
    """Все колонки строки одной строкой: проверка «открытого секрета в БД нет»."""
    return " ".join(str(getattr(row, column.name)) for column in row.__table__.columns)


async def audit_of(world: World, name: str, entity_id: Any = None) -> list[AuditLog]:
    return await world.audit(bot_action(name), entity_id)


# --- ключи ---------------------------------------------------------------------------------------------------------


async def test_key_is_stored_only_as_a_sha256_hash(world: World) -> None:
    key, secret = await create_key(world.session)
    assert key.key_hash == hash_token(secret)
    assert secret not in column_values(key)


async def test_key_prefix_is_the_first_eight_characters(world: World) -> None:
    key, secret = await create_key(world.session)
    assert len(key.key_prefix) == 8 and key.key_prefix == secret[:8]


async def test_every_key_is_different_and_long(world: World) -> None:
    first, first_secret = await create_key(world.session, "one")
    second, second_secret = await create_key(world.session, "two")
    assert first_secret != second_secret and first.key_hash != second.key_hash
    assert len(first_secret) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", first_secret)


async def test_a_new_key_is_unrevoked_and_never_seen(world: World) -> None:
    key, _ = await create_key(world.session)
    assert key.revoked_at is None and key.last_seen_at is None
    assert key.bot_username is None and key.last_seen_version is None


async def test_key_remembers_who_created_it(world: World) -> None:
    admin = await make_admin(world.session)
    key, _ = await create_key(world.session, actor=AuditActor(admin_id=admin.admin.id))
    assert key.created_by == admin.admin.id
    (event,) = await audit_of(world, "bot_key_created", key.id)
    assert event.admin_id == admin.admin.id


async def test_key_name_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        BotKeyCreate(name="")


async def test_key_creation_is_audited_without_the_key(world: World) -> None:
    key, secret = await create_key(world.session)
    (event,) = await audit_of(world, "bot_key_created", key.id)
    assert event.entity_type == "bot_key"
    assert secret not in await audit_dump() and key.key_hash not in await audit_dump()


async def test_a_generated_key_is_masked_by_the_log_safety_net(world: World) -> None:
    """Ключ узнаётся по префиксу: даже вне заголовка и без имени поля он не уходит в лог."""
    _, secret = await create_key(world.session)
    assert secret not in redact(f"oops {secret} oops")


async def test_revoking_a_key_sets_revoked_at_and_audits_it(world: World) -> None:
    key, secret = await create_key(world.session)
    revoked = await BotKeyService(world.session, AuditActor(admin_id=None)).revoke(key.id)
    assert revoked.revoked_at is not None
    (event,) = await audit_of(world, "bot_key_revoked", key.id)
    assert secret not in str(event.meta)


async def test_revoking_twice_keeps_the_first_time_and_one_audit_event(world: World) -> None:
    key, _ = await create_key(world.session)
    service = BotKeyService(world.session, AuditActor(admin_id=None))
    first = (await service.revoke(key.id)).revoked_at
    assert (await service.revoke(key.id)).revoked_at == first
    assert len(await audit_of(world, "bot_key_revoked", key.id)) == 1


async def test_revoking_an_unknown_key_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await BotKeyService(world.session, AuditActor(admin_id=None)).revoke(uuid4())


async def test_key_list_shows_revoked_keys_too(world: World) -> None:
    active, _ = await create_key(world.session, "active")
    revoked, _ = await create_key(world.session, "revoked")
    service = BotKeyService(world.session, AuditActor(admin_id=None))
    await service.revoke(revoked.id)
    listed = {item.id: item for item in await service.list()}
    assert listed[active.id].revoked_at is None and listed[revoked.id].revoked_at is not None


# --- приглашения ---------------------------------------------------------------------------------------------------


async def test_invite_is_stored_only_as_a_hash(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    assert invite.code_hash == hash_token(code)
    assert code not in column_values(invite)


async def test_invite_code_fits_the_telegram_start_parameter(world: World) -> None:
    scene = await bot_scene(world)
    _, code = await create_invite(world.session, scene.protocol.id)
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,64}", code)


async def test_invite_keeps_protocol_days_and_deadline(world: World) -> None:
    scene = await bot_scene(world)
    invite, _ = await create_invite(world.session, scene.protocol.id, days=45, expires_in=timedelta(days=3))
    assert (invite.protocol_id, invite.days) == (scene.protocol.id, 45)
    assert invite.used_at is None and invite.revoked_at is None
    assert abs(invite.expires_at - (now() + timedelta(days=3))) < timedelta(minutes=1)


async def test_invite_creation_is_audited_without_the_code(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    (event,) = await audit_of(world, "bot_invite_created", invite.id)
    assert event.entity_type == "bot_invite"
    assert code not in await audit_dump() and invite.code_hash not in await audit_dump()


async def test_invite_for_an_unknown_protocol_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await create_invite(world.session, uuid4())


async def test_invite_already_expired_at_creation_is_refused(world: World) -> None:
    scene = await bot_scene(world)
    with pytest.raises((InvalidDataError, ValidationError)):
        await create_invite(world.session, scene.protocol.id, expires_in=timedelta(seconds=-1))


async def test_check_reports_valid_for_a_fresh_invite_and_changes_nothing(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    logs_before = len((await world.session.execute(select(AuditLog))).scalars().all())
    assert await check_invite(world.session, code) == VALID
    assert await check_invite(world.session, code) == VALID  # проверка не «тратит» приглашение
    await world.session.refresh(invite)
    assert invite.used_at is None
    assert len((await world.session.execute(select(AuditLog))).scalars().all()) == logs_before


async def test_check_reports_unknown_for_a_code_that_never_existed(world: World) -> None:
    assert await check_invite(world.session, "no-such-code-at-all") == UNKNOWN


async def test_check_reports_expired_after_the_deadline(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    invite.expires_at = now() - timedelta(seconds=1)
    await world.session.commit()
    assert await check_invite(world.session, code) == EXPIRED


async def test_check_reports_used_after_a_registration(world: World) -> None:
    scene = await bot_scene(world)
    _, code = await create_invite(world.session, scene.protocol.id)
    await register(world.session, code, key=scene.key)
    assert await check_invite(world.session, code) == USED


async def test_check_reports_revoked_after_revoke(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    await BotInviteService(world.session, AuditActor(admin_id=None)).revoke(invite.id)
    assert await check_invite(world.session, code) in (REVOKED, UNKNOWN)  # отозванное можно не отличать от «нет такого»


async def test_revoking_an_invite_is_idempotent_and_audited_once(world: World) -> None:
    scene = await bot_scene(world)
    invite, code = await create_invite(world.session, scene.protocol.id)
    service = BotInviteService(world.session, AuditActor(admin_id=None))
    first = (await service.revoke(invite.id)).revoked_at
    assert first is not None and (await service.revoke(invite.id)).revoked_at == first
    (event,) = await audit_of(world, "bot_invite_revoked", invite.id)
    assert code not in str(event.meta)


async def test_revoking_an_unknown_invite_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        await BotInviteService(world.session, AuditActor(admin_id=None)).revoke(uuid4())


async def test_invite_list_has_every_invite(world: World) -> None:
    scene = await bot_scene(world)
    first, _ = await create_invite(world.session, scene.protocol.id, note="для Ивана")
    second, _ = await create_invite(world.session, scene.protocol.id)
    listed = {item.id for item in await BotInviteService(world.session, AuditActor(admin_id=None)).list()}
    assert {first.id, second.id} <= listed


async def test_deleting_the_protocol_keeps_the_invite_without_a_protocol(world: World) -> None:
    from sqlalchemy import text

    scene = await bot_scene(world)
    invite, _ = await create_invite(world.session, scene.protocol.id)
    async with session_factory() as other:
        await other.execute(text("DELETE FROM protocols WHERE id = :id"), {"id": scene.protocol.id})
        await other.commit()
    await world.session.refresh(invite)
    assert invite.protocol_id is None  # FK SET NULL: приглашение переживает протокол, но выдать по нему уже нельзя
