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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import hash_token
from infrastructure.workers.worker import JobWorker
from main import app
from modules.bot.models.invite import BotInvite
from tests.bot_client_support import Conflict, PanelAuthError, PanelClient, RateLimited, Rejected, outbox_result
from tests.bot_support import (
    CONFIG_UNAVAILABLE,
    EMAIL,
    KEYS_URL,
    NAME,
    NEWS_MARKER,
    NEWS_TEXT,
    NO_ACCESS,
    NOT_CONFIGURED,
    NOT_READY,
    PUBLIC_BASE,
    PUBLIC_CONFIG_URL,
    SECOND_EMAIL,
    SECOND_TELEGRAM_ID,
    TELEGRAM_ID,
    USERNAME,
    add_people,
    admin_create_broadcast,
    admin_create_invite,
    admin_create_key,
    audit_dump,
    set_public_address,
    token_of,
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
    assert created.status == "provisioning" and getattr(created, "config_url", None) is None  # URL даёт config-link

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


# --- ссылка на страницу конфига (25.3) -------------------------------------------------------------------------------


async def active_person(scene: Scene, worker: JobWorker) -> None:
    await scene.register(await scene.invite())
    await drain(worker)


async def test_config_link_gives_a_url_with_the_token_in_the_fragment_and_the_end_date(
    scene: Scene, worker: JobWorker
) -> None:
    await active_person(scene, worker)

    link = await scene.client.config_link(TELEGRAM_ID)

    assert link is not None
    assert len(token_of(link.config_url)) == 43 and link.config_url.startswith(f"{PUBLIC_BASE}/c#")
    until = datetime.fromisoformat(str(link.valid_until).replace("Z", "+00:00"))
    assert abs((until - (datetime.now(timezone.utc) + timedelta(days=30))).total_seconds()) < 300


async def test_the_url_from_config_link_opens_the_page_and_a_new_one_closes_it(scene: Scene, worker: JobWorker) -> None:
    await active_person(scene, worker)
    first = await scene.client.config_link(TELEGRAM_ID)
    second = await scene.client.config_link(TELEGRAM_ID)
    assert first is not None and second is not None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as person:
        old = await person.post(PUBLIC_CONFIG_URL, json={"token": token_of(first.config_url)})
        new = await person.post(PUBLIC_CONFIG_URL, json={"token": token_of(second.config_url)})

    assert old.status_code == 404 and new.status_code == 200


async def test_config_link_of_an_unknown_telegram_is_none(scene: Scene) -> None:
    assert await scene.client.config_link(TELEGRAM_ID) is None


async def test_config_link_while_the_client_is_being_issued_is_not_ready(scene: Scene) -> None:
    await scene.register(await scene.invite())  # воркер не запускался

    with pytest.raises(Conflict) as caught:
        await scene.client.config_link(TELEGRAM_ID)

    assert caught.value.reason == NOT_READY


async def test_config_link_after_the_access_was_revoked_is_no_access(
    scene: Scene, worker: JobWorker, session: AsyncSession
) -> None:
    await active_person(scene, worker)
    grant_id = (await session.execute(text("SELECT id FROM access_grants"))).scalar_one()
    assert (await scene.api.post(f"/api/v1/access/{grant_id}/revoke")).status_code == 200

    with pytest.raises(Conflict) as caught:
        await scene.client.config_link(TELEGRAM_ID)

    assert caught.value.reason == NO_ACCESS


async def test_config_link_of_an_imported_client_is_config_unavailable(
    scene: Scene, worker: JobWorker, session: AsyncSession
) -> None:
    await active_person(scene, worker)
    await session.execute(text("UPDATE vpn_credentials SET source = 'IMPORTED', encrypted_config = NULL"))
    await session.commit()

    with pytest.raises(Conflict) as caught:
        await scene.client.config_link(TELEGRAM_ID)

    assert caught.value.reason == CONFIG_UNAVAILABLE


async def test_config_link_without_a_public_address_is_not_configured(
    scene: Scene, worker: JobWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    await active_person(scene, worker)
    set_public_address(monkeypatch, "", domain="")

    with pytest.raises(Conflict) as caught:
        await scene.client.config_link(TELEGRAM_ID)

    assert caught.value.reason == NOT_CONFIGURED


async def test_config_link_with_a_telegram_id_out_of_range_is_rejected_by_field(scene: Scene) -> None:
    with pytest.raises(Rejected) as caught:
        await scene.client.config_link(0)

    assert caught.value.field == "telegram_id"


async def test_a_revoked_key_cannot_ask_for_a_link(scene: Scene, worker: JobWorker) -> None:
    await active_person(scene, worker)
    assert (await scene.api.post(f"{KEYS_URL}/{scene.key['id']}/revoke")).status_code == 200

    with pytest.raises(PanelAuthError):
        await scene.client.config_link(TELEGRAM_ID)


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


async def test_the_token_of_a_link_is_not_in_the_audit(scene: Scene, worker: JobWorker) -> None:
    await active_person(scene, worker)
    link = await scene.client.config_link(TELEGRAM_ID)
    assert link is not None

    dump = await audit_dump()
    assert token_of(link.config_url) not in dump and hash_token(token_of(link.config_url)) not in dump


# --- очередь сообщений (25.4) ----------------------------------------------------------------------------------------


async def person_id(session: AsyncSession, telegram_id: int = TELEGRAM_ID) -> str:
    query = text("SELECT id FROM vpn_users WHERE telegram_id = :t")
    return str((await session.execute(query, {"t": telegram_id})).scalar_one())


async def test_claim_gives_the_queued_news_with_its_text_and_ack_closes_the_rows(
    scene: Scene, session: AsyncSession
) -> None:
    await add_people(session, 2)
    created = await admin_create_broadcast(scene.api)

    claim = await scene.client.claim(50)

    assert claim.lease_id and claim.lease_until and len(claim.items) == 2
    assert {item.kind for item in claim.items} == {"news"}
    assert {item.broadcast_id for item in claim.items} == {created["id"]}
    assert all(isinstance(item.telegram_id, int) for item in claim.items)
    assert claim.texts == {created["id"]: NEWS_TEXT}
    outcome = await scene.client.ack(claim.lease_id, [outbox_result(item.id, "sent") for item in claim.items])
    assert sorted(outcome.applied) == sorted(item.id for item in claim.items) and list(outcome.ignored) == []
    done = (await scene.api.get(f"/api/v1/bot-admin/broadcasts/{created['id']}")).json()
    assert done["sent"] == 2 and done["state"] == "finished"


async def test_claim_of_an_empty_queue_is_an_empty_claim_not_an_error(scene: Scene) -> None:
    claim = await scene.client.claim(50)

    assert list(claim.items) == [] and dict(claim.texts) == {}


async def test_claim_gives_a_working_link_for_a_config_message(
    scene: Scene, worker: JobWorker, session: AsyncSession
) -> None:
    await active_person(scene, worker)
    await admin_create_broadcast(scene.api, kind="config", target="selected", user_ids=[await person_id(session)])

    claim = await scene.client.claim(50)

    (item,) = claim.items
    assert item.kind == "config" and item.telegram_id == TELEGRAM_ID and item.broadcast_id is not None
    assert item.config_url.startswith(f"{PUBLIC_BASE}/c#") and len(token_of(item.config_url)) == 43
    until = datetime.fromisoformat(str(item.valid_until).replace("Z", "+00:00"))
    assert abs((until - (datetime.now(timezone.utc) + timedelta(days=30))).total_seconds()) < 300
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as person:
        assert (await person.post(PUBLIC_CONFIG_URL, json={"token": token_of(item.config_url)})).status_code == 200
    assert claim.texts == {}


async def test_the_address_of_a_claimed_link_stays_out_of_the_repr_of_the_item_and_of_the_claim(
    scene: Scene, worker: JobWorker, session: AsyncSession
) -> None:
    await active_person(scene, worker)
    await admin_create_broadcast(scene.api, kind="config", target="selected", user_ids=[await person_id(session)])

    claim = await scene.client.claim(50)

    (item,) = claim.items
    token = token_of(item.config_url)
    assert token not in repr(item) and "/c#" not in repr(item)
    assert token not in repr(claim) and token not in str(claim)


async def test_ack_with_a_lease_the_panel_did_not_give_ignores_everything(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 2)
    await admin_create_broadcast(scene.api)
    claim = await scene.client.claim(50)
    results = [outbox_result(item.id, "sent") for item in claim.items]

    outcome = await scene.client.ack("00000000-0000-4000-8000-000000000000", results)

    assert list(outcome.applied) == [] and sorted(outcome.ignored) == sorted(item.id for item in claim.items)


async def test_ack_blocked_marks_the_person_and_failed_keeps_the_code(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 2)
    created = await admin_create_broadcast(scene.api)
    claim = await scene.client.claim(50)
    first, second = claim.items

    await scene.client.ack(
        claim.lease_id,
        [outbox_result(first.id, "blocked", "blocked"), outbox_result(second.id, "failed", "chat_not_found")],
    )

    done = (await scene.api.get(f"/api/v1/bot-admin/broadcasts/{created['id']}")).json()
    assert (done["blocked"], done["failed"], done["sent"]) == (1, 1, 0)
    marked = (await session.execute(text("SELECT telegram_id FROM vpn_users WHERE bot_blocked_at IS NOT NULL"))).all()
    assert [row[0] for row in marked] == [first.telegram_id]


async def test_ack_retry_puts_the_row_back_and_a_later_claim_gives_it_again(
    scene: Scene, session: AsyncSession
) -> None:
    await add_people(session, 1)
    await admin_create_broadcast(scene.api)
    first = await scene.client.claim(50)

    await scene.client.ack(first.lease_id, [outbox_result(first.items[0].id, "retry", "network", 30)])

    assert list((await scene.client.claim(50)).items) == []  # ждёт 30 с
    await session.execute(text("UPDATE bot_outbox SET not_before = now() - interval '1 second'"))
    await session.commit()
    again = await scene.client.claim(50)
    assert [item.id for item in again.items] == [first.items[0].id] and again.lease_id != first.lease_id


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_claim_with_a_limit_the_panel_refuses_is_rejected_by_field(scene: Scene, limit: int) -> None:
    with pytest.raises(Rejected) as caught:
        await scene.client.claim(limit)

    assert caught.value.field == "limit"


async def test_ack_with_an_error_that_is_not_a_code_is_rejected_and_the_text_is_not_kept(
    scene: Scene, session: AsyncSession
) -> None:
    await add_people(session, 1)
    await admin_create_broadcast(scene.api)
    claim = await scene.client.claim(50)
    secret_text = "Bad Request: chat not found for 5550001234"

    with pytest.raises(Rejected) as caught:
        await scene.client.ack(claim.lease_id, [outbox_result(claim.items[0].id, "failed", secret_text)])

    assert secret_text not in str(caught.value) and "5550001234" not in str(caught.value)


async def test_a_revoked_key_can_neither_claim_nor_ack(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 1)
    await admin_create_broadcast(scene.api)
    claim = await scene.client.claim(50)
    assert (await scene.api.post(f"{KEYS_URL}/{scene.key['id']}/revoke")).status_code == 200

    with pytest.raises(PanelAuthError):
        await scene.client.claim(50)
    with pytest.raises(PanelAuthError):
        await scene.client.ack(claim.lease_id, [outbox_result(claim.items[0].id, "sent")])


async def test_the_time_of_the_last_claim_is_shown_to_the_admin(scene: Scene) -> None:
    assert (await scene.api.get("/api/v1/bot-admin/status")).json()["last_claim_at"] is None

    await scene.client.claim(1)

    assert (await scene.api.get("/api/v1/bot-admin/status")).json()["last_claim_at"] is not None


async def test_the_claimed_link_and_the_news_text_are_not_in_the_audit(
    scene: Scene, worker: JobWorker, session: AsyncSession
) -> None:
    await active_person(scene, worker)
    await admin_create_broadcast(scene.api, kind="config", target="selected", user_ids=[await person_id(session)])
    await admin_create_broadcast(scene.api)
    claim = await scene.client.claim(50)
    await scene.client.ack(claim.lease_id, [outbox_result(item.id, "sent") for item in claim.items])

    dump = await audit_dump()

    link = next(item for item in claim.items if item.kind == "config")
    assert token_of(link.config_url) not in dump and NEWS_MARKER not in dump and str(TELEGRAM_ID) not in dump
