"""Контракт «бот - панель» (этап 25.2): настоящий `PanelClient` из `bot/src/tollwire_bot/panel.py` против настоящего API
(`httpx.ASGITransport`, настоящие БД, Redis, воркер; SSH фейковый).

`bot/tests` проверяют клиент против фейковой панели; здесь - что фейк не врёт: каждый метод клиента, каждый `reason`
409, 422 с именем поля, 401 (неверный и отозванный ключ), 429. Если панель изменит ответ, красным станет этот файл."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import hash_token
from infrastructure.workers.worker import JobWorker
from main import app
from modules.bot.models.invite import BotInvite
from tests.bot_client_support import Conflict, PanelAuthError, PanelClient, RateLimited, Rejected
from tests.bot_support import (
    EMAIL,
    KEYS_URL,
    NAME,
    SECOND_EMAIL,
    SECOND_TELEGRAM_ID,
    TELEGRAM_ID,
    USERNAME,
    admin_create_invite,
    admin_create_key,
    audit_dump,
)
from tests.fixtures import World
from tests.integration.conftest import drain

BOT_NAME = "tollwire_contract_bot"


@dataclass
class Naps:
    """Вместо asyncio.sleep клиента: записывает паузы и не ждёт."""

    delays: list[float] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@dataclass
class Scene:
    api: httpx.AsyncClient
    client: PanelClient
    key: dict[str, Any]
    naps: Naps
    protocol_id: Any

    async def invite(self, **options: Any) -> str:
        return str((await admin_create_invite(self.api, self.protocol_id, **options))["code"])

    async def register(self, code: str, /, **fields: Any) -> Any:
        values: dict[str, Any] = {
            "code": code,
            "telegram_id": TELEGRAM_ID,
            "telegram_username": USERNAME,
            "name": NAME,
            "email": EMAIL,
        }
        return await self.client.register(**{**values, **fields})


def make_client(key: str, naps: Naps) -> PanelClient:
    return PanelClient("http://testserver", key, transport=httpx.ASGITransport(app=app), sleep=naps)


@pytest.fixture
async def scene(api: httpx.AsyncClient, world: World) -> AsyncIterator[Scene]:
    _, protocol, _ = await world.server()
    assert protocol is not None
    key, naps = await admin_create_key(api), Naps()
    client = make_client(key["key"], naps)
    try:
        yield Scene(api, client, key, naps, protocol.id)
    finally:
        await client.aclose()


# --- heartbeat -------------------------------------------------------------------------------------------------------


async def test_heartbeat_is_recorded_for_the_key(scene: Scene) -> None:
    await scene.client.heartbeat(BOT_NAME, "0.1.0")

    (listed,) = (await scene.api.get(KEYS_URL)).json()
    assert listed["bot_username"] == BOT_NAME and listed["last_seen_version"] == "0.1.0"
    assert listed["last_seen_at"] is not None


async def test_heartbeat_with_a_name_the_panel_refuses_is_rejected_by_field(scene: Scene) -> None:
    with pytest.raises(Rejected) as caught:
        await scene.client.heartbeat("bad name!", "0.1.0")

    assert caught.value.field == "bot_username"


# --- приглашения -----------------------------------------------------------------------------------------------------


async def test_a_fresh_invitation_is_valid(scene: Scene) -> None:
    assert await scene.client.check_invite(await scene.invite()) == "valid"


async def test_an_unknown_code_is_unknown_not_an_error(scene: Scene) -> None:
    assert await scene.client.check_invite("no-such-code-at-all-0000") == "unknown"


async def test_a_used_invitation_is_used(scene: Scene) -> None:
    code = await scene.invite()
    await scene.register(code)

    assert await scene.client.check_invite(code) == "used"


async def test_a_revoked_invitation_is_revoked(scene: Scene) -> None:
    invite = await admin_create_invite(scene.api, scene.protocol_id)
    assert (await scene.api.post(f"/api/v1/bot-admin/invites/{invite['id']}/revoke")).status_code == 200

    assert await scene.client.check_invite(invite["code"]) == "revoked"


async def test_an_overdue_invitation_is_expired(scene: Scene, session: AsyncSession) -> None:
    code = await scene.invite()
    await age(session, code)

    assert await scene.client.check_invite(code) == "expired"


async def age(session: AsyncSession, code: str) -> None:
    invite = (await session.execute(select(BotInvite).where(BotInvite.code_hash == hash_token(code)))).scalar_one()
    invite.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await session.commit()


# --- регистрация -----------------------------------------------------------------------------------------------------


async def test_registration_goes_from_provisioning_to_active_and_shows_up_in_status(
    scene: Scene, worker: JobWorker
) -> None:
    code = await scene.invite(days=30)

    created = await scene.register(code)
    assert created.status == "provisioning" and created.config_url is None

    assert await drain(worker) >= 1  # воркер выдаёт клиента на (фейковом) сервере
    registration = await scene.client.registration(created.registration_id)
    assert registration is not None and registration.status == "active"

    user = await scene.client.user_by_telegram(TELEGRAM_ID)
    assert user is not None and user.name == NAME and user.protocol and user.valid_until
    until = datetime.fromisoformat(str(user.valid_until).replace("Z", "+00:00"))
    assert abs((until - (datetime.now(timezone.utc) + timedelta(days=30))).total_seconds()) < 300


async def test_the_same_person_repeating_the_request_gets_the_same_registration(scene: Scene) -> None:
    """Клиент повторяет POST после обрыва: панель обязана вернуть ту же регистрацию, а не создать вторую."""
    code = await scene.invite()

    first = await scene.register(code)
    second = await scene.register(code)

    assert first.registration_id == second.registration_id


async def test_a_name_with_unicode_and_a_long_telegram_id_survive_the_round_trip(
    scene: Scene, worker: JobWorker
) -> None:
    big = 2**62 + 12345
    await scene.register(await scene.invite(), telegram_id=big, name="Иван Петров 🙂")
    await drain(worker)

    user = await scene.client.user_by_telegram(big)
    assert user is not None and user.name == "Иван Петров 🙂"


async def test_an_unknown_registration_and_an_unknown_user_are_none(scene: Scene) -> None:
    assert await scene.client.registration("00000000-0000-4000-8000-000000000000") is None
    assert await scene.client.user_by_telegram(TELEGRAM_ID) is None


# --- 409 -------------------------------------------------------------------------------------------------------------


async def test_unknown_invitation_is_invite_invalid(scene: Scene) -> None:
    with pytest.raises(Conflict) as caught:
        await scene.register("no-such-code-at-all-0000")

    assert caught.value.reason == "invite_invalid"


async def test_revoked_invitation_is_invite_invalid(scene: Scene) -> None:
    invite = await admin_create_invite(scene.api, scene.protocol_id)
    await scene.api.post(f"/api/v1/bot-admin/invites/{invite['id']}/revoke")

    with pytest.raises(Conflict) as caught:
        await scene.register(invite["code"])

    assert caught.value.reason == "invite_invalid"


async def test_overdue_invitation_is_invite_expired(scene: Scene, session: AsyncSession) -> None:
    code = await scene.invite()
    await age(session, code)

    with pytest.raises(Conflict) as caught:
        await scene.register(code)

    assert caught.value.reason == "invite_expired"


async def test_invitation_taken_by_someone_else_is_invite_used(scene: Scene) -> None:
    code = await scene.invite()
    await scene.register(code)

    with pytest.raises(Conflict) as caught:
        await scene.register(code, telegram_id=SECOND_TELEGRAM_ID, email=SECOND_EMAIL)

    assert caught.value.reason == "invite_used"


async def test_a_registered_telegram_account_is_telegram_taken(scene: Scene) -> None:
    await scene.register(await scene.invite())

    with pytest.raises(Conflict) as caught:
        await scene.register(await scene.invite(), email=SECOND_EMAIL)

    assert caught.value.reason == "telegram_taken"


async def test_a_taken_email_is_email_taken_in_any_letter_case(scene: Scene) -> None:
    await scene.register(await scene.invite())

    with pytest.raises(Conflict) as caught:
        await scene.register(await scene.invite(), telegram_id=SECOND_TELEGRAM_ID, email=EMAIL.upper())

    assert caught.value.reason == "email_taken"


async def test_a_refused_registration_does_not_burn_the_invitation(scene: Scene) -> None:
    code = await scene.invite()
    await scene.register(await scene.invite())

    with pytest.raises(Conflict):
        await scene.register(code, telegram_id=SECOND_TELEGRAM_ID, email=EMAIL)  # почта занята
    assert await scene.client.check_invite(code) == "valid"  # человек введёт другую и пойдёт дальше с тем же кодом

    created = await scene.register(code, telegram_id=SECOND_TELEGRAM_ID, email=SECOND_EMAIL)
    assert created.status == "provisioning"


# --- 422 -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"email": "not-an-email"}, "email"),
        ({"name": "Ив\x00ан"}, "name"),
        ({"name": "Ив‮ан"}, "name"),
        ({"name": "x" * 65}, "name"),
        ({"telegram_id": 0}, "telegram_id"),
        ({"telegram_username": "a\nb"}, "telegram_username"),
        ({"code": "c" * 65}, "code"),
    ],
    ids=["email", "NUL in name", "RLO in name", "long name", "telegram id", "newline in username", "long code"],
)
async def test_invalid_input_is_rejected_with_the_field_name(
    scene: Scene, fields: dict[str, Any], expected: str
) -> None:
    code = await scene.invite()

    with pytest.raises(Rejected) as caught:
        await scene.register(code, **fields)

    assert caught.value.field == expected
    assert EMAIL not in str(caught.value) and NAME not in str(caught.value)
    assert await scene.client.check_invite(code) == "valid"  # отказ приглашение не тратит


async def test_a_registration_id_that_is_not_a_uuid_is_rejected_by_field(scene: Scene) -> None:
    with pytest.raises(Rejected) as caught:
        await scene.client.registration("not-a-uuid")

    assert caught.value.field == "registration_id"


async def test_a_telegram_id_outside_the_range_is_rejected_by_field(scene: Scene) -> None:
    with pytest.raises(Rejected) as caught:
        await scene.client.user_by_telegram(0)

    assert caught.value.field == "telegram_id"


# --- 401 -------------------------------------------------------------------------------------------------------------


async def test_a_wrong_key_is_an_auth_error_without_retries(scene: Scene) -> None:
    other = make_client("twb_" + "x" * 43, scene.naps)
    try:
        with pytest.raises(PanelAuthError):
            await other.heartbeat(BOT_NAME, "0.1.0")
    finally:
        await other.aclose()

    assert scene.naps.delays == []


async def test_a_revoked_key_stops_working_on_the_next_request(scene: Scene) -> None:
    await scene.client.heartbeat(BOT_NAME, "0.1.0")
    assert (await scene.api.post(f"{KEYS_URL}/{scene.key['id']}/revoke")).status_code == 200

    with pytest.raises(PanelAuthError):
        await scene.client.heartbeat(BOT_NAME, "0.1.0")
    with pytest.raises(PanelAuthError):
        await scene.client.user_by_telegram(TELEGRAM_ID)


# --- 429 -------------------------------------------------------------------------------------------------------------


async def test_registration_rate_limit_is_rate_limited_without_waiting_a_minute(scene: Scene) -> None:
    """Лимит регистраций - 10 в минуту на ключ, Retry-After около минуты: клиент не ждёт (потолок ожидания 10 с)."""
    limited_at = None
    for attempt in range(1, 40):
        try:
            await scene.register("no-such-code-at-all-0000", telegram_id=TELEGRAM_ID + attempt)
        except Conflict as refused:
            assert refused.reason == "invite_invalid"
        except RateLimited:
            limited_at = attempt
            break

    assert limited_at is not None, "the registration limit must reach the client as RateLimited"
    assert scene.naps.delays == []  # долгий Retry-After не отсиживается
    await scene.client.heartbeat(BOT_NAME, "0.1.0")  # остальное API ключу по-прежнему доступно


# --- секреты ---------------------------------------------------------------------------------------------------------


async def test_the_key_is_not_in_the_audit_after_a_whole_conversation(scene: Scene) -> None:
    code = await scene.invite()
    await scene.client.heartbeat(BOT_NAME, "0.1.0")
    await scene.register(code)

    dump = await audit_dump()
    assert scene.key["key"] not in dump and code not in dump
