"""Рассылка от админа до подтверждения ботом, сквозной сценарий (этап 25.4).

Админ ставит рассылку через HTTP, «бот» забирает и подтверждает пачки настоящим `PanelClient` через `ASGITransport`
(тот же код, что в боте), итоги видны админу. Главное: рассылка на 1000 человек укладывается в лимит запросов бота
(300 в минуту на ключ), заблокировавшие бота помечены, ссылка на конфиг выпускается только при выдаче и нигде не
хранится и не пишется в логи."""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import hash_token
from infrastructure.workers.worker import JobWorker
from main import app
from tests.bot_client_support import PanelClient, outbox_result
from tests.bot_support import (
    BROADCASTS_URL,
    CONFIG,
    NEWS_MARKER,
    NEWS_TEXT,
    OUTBOX_ACK_URL,
    OUTBOX_CLAIM_URL,
    PUBLIC_BASE,
    PUBLIC_CONFIG_URL,
    TARGET_PROTOCOL,
    TARGET_SELECTED,
    TELEGRAM_ID,
    ack_result,
    add_people,
    admin_create_broadcast,
    admin_create_key,
    audit_dump,
    bot_client,
    broadcast_url,
    http_ack,
    http_activate,
    http_claim,
    sql_rows,
    tables_dump,
    token_of,
    user_config_link_url,
)
from tests.fixtures import World

FIRST_TELEGRAM_ID = 7_000_000_000
PEOPLE = 1000
BATCH = 100
BOT_KEY_REQUESTS_PER_MINUTE = 300


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
    bot: httpx.AsyncClient
    key: dict[str, Any]
    naps: Naps
    protocol_id: UUID
    worker: JobWorker


@pytest.fixture
async def scene(api: httpx.AsyncClient, world: World, worker: JobWorker) -> AsyncIterator[Scene]:
    _, protocol, _ = await world.server()
    assert protocol is not None
    key, naps = await admin_create_key(api), Naps()
    client = PanelClient("http://testserver", key["key"], transport=httpx.ASGITransport(app=app), sleep=naps)
    async with bot_client(key["key"]) as bot:
        try:
            yield Scene(api, client, bot, key, naps, protocol.id, worker)
        finally:
            await client.aclose()


def outcome_of(telegram_id: int) -> tuple[str, str | None]:
    """Что «Telegram» ответил этому человеку: каждый 97-й заблокировал бота, каждый 101-й - чата нет."""
    if telegram_id % 97 == 0:
        return "blocked", "blocked"
    if telegram_id % 101 == 0:
        return "failed", "chat_not_found"
    return "sent", None


EXPECTED = {"sent": 0, "blocked": 0, "failed": 0}
for _index in range(PEOPLE):
    EXPECTED[outcome_of(FIRST_TELEGRAM_ID + _index)[0]] += 1


async def run_the_bot(scene: Scene, limit: int = BATCH) -> tuple[list[Any], int]:
    """Бот забирает пачки до пустой очереди и подтверждает каждую; возвращает все выданные строки и число запросов."""
    delivered: list[Any] = []
    requests = 0
    while True:
        claim = await scene.client.claim(limit)
        requests += 1
        if not claim.items:
            return delivered, requests
        results = []
        for item in claim.items:
            status, error = outcome_of(item.telegram_id)
            results.append(outbox_result(item.id, status, error))
        await scene.client.ack(claim.lease_id, results)
        requests += 1
        delivered.extend(claim.items)


async def activate(scene: Scene, count: int) -> list[str]:
    """Люди, зарегистрированные через бота, с клиентами на фейковом сервере; id в панели."""
    ids = []
    for number in range(count):
        telegram_id = TELEGRAM_ID + number
        await http_activate(
            scene.api,
            scene.bot,
            scene.protocol_id,
            scene.worker,
            telegram_id=telegram_id,
            email=f"person{number}@example.org",
            name=f"Person {number}",
        )
        ids.append(str((await sql_rows("SELECT id FROM vpn_users WHERE telegram_id = :t", t=telegram_id))[0][0]))
    return ids


# --- рассылка на 1000 человек --------------------------------------------------------------------------------------


async def test_a_news_broadcast_to_a_thousand_people_is_delivered_in_batches_and_the_totals_add_up(
    scene: Scene, session: AsyncSession
) -> None:
    await add_people(session, PEOPLE, first_telegram_id=FIRST_TELEGRAM_ID)
    created = await admin_create_broadcast(scene.api)
    assert created["total"] == PEOPLE and created["state"] == "running"

    delivered, requests = await run_the_bot(scene)

    assert len(delivered) == PEOPLE and len({item.telegram_id for item in delivered}) == PEOPLE  # каждому по разу
    assert requests <= 2 * (PEOPLE // BATCH) + 1 and requests < BOT_KEY_REQUESTS_PER_MINUTE  # лимит ключа не задет
    assert scene.naps.delays == []  # 429 не было
    done = (await scene.api.get(broadcast_url(created["id"]))).json()
    assert (done["sent"], done["blocked"], done["failed"]) == (
        EXPECTED["sent"],
        EXPECTED["blocked"],
        EXPECTED["failed"],
    )
    assert done["skipped"] == done["cancelled"] == 0 and done["total"] == PEOPLE
    assert done["state"] == "finished" and done["finished_at"] is not None


async def test_the_people_who_blocked_the_bot_are_marked_and_left_out_of_the_next_broadcast(
    scene: Scene, session: AsyncSession
) -> None:
    await add_people(session, PEOPLE, first_telegram_id=FIRST_TELEGRAM_ID)
    await admin_create_broadcast(scene.api)
    await run_the_bot(scene)

    marked = await sql_rows("SELECT telegram_id FROM vpn_users WHERE bot_blocked_at IS NOT NULL")
    second = await admin_create_broadcast(scene.api)

    assert {row[0] for row in marked} == {
        FIRST_TELEGRAM_ID + index for index in range(PEOPLE) if outcome_of(FIRST_TELEGRAM_ID + index)[0] == "blocked"
    }
    assert second["total"] == PEOPLE - EXPECTED["blocked"]


async def test_a_claim_answer_has_the_agreed_shape_and_is_not_cached(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 150, first_telegram_id=FIRST_TELEGRAM_ID)
    created = await admin_create_broadcast(scene.api)

    response = await http_claim(scene.bot, BATCH)

    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    body = response.json()
    assert set(body) == {"lease_id", "lease_until", "items", "texts"}
    assert len(body["items"]) == BATCH
    assert all(set(item) == {"id", "kind", "telegram_id", "broadcast_id"} for item in body["items"])
    assert {item["kind"] for item in body["items"]} == {"news"}
    assert body["texts"] == {created["id"]: NEWS_TEXT}  # текст один на всю пачку
    until = datetime.fromisoformat(body["lease_until"])
    assert timedelta(seconds=30) < until - datetime.now(timezone.utc) <= timedelta(seconds=65)


async def test_an_empty_claim_is_200_with_nothing_in_it(scene: Scene) -> None:
    response = await http_claim(scene.bot)

    assert response.status_code == 200 and response.json()["items"] == [] and response.json()["texts"] == {}


@pytest.mark.parametrize("body", [{"limit": 0}, {"limit": 101}, {"limit": -5}, {"limit": "many"}, {"limit": None}])
async def test_a_claim_limit_outside_1_to_100_is_422(scene: Scene, body: dict[str, Any]) -> None:
    assert (await scene.bot.post(OUTBOX_CLAIM_URL, json=body)).status_code == 422


LEASE = "00000000-0000-4000-8000-000000000000"
ITEM = "00000000-0000-4000-8000-000000000001"
INVALID_ACKS = [
    ({}, "empty"),
    ({"lease_id": "not-a-uuid", "results": []}, "lease not a uuid"),
    ({"lease_id": LEASE}, "no results"),
    ({"lease_id": LEASE, "results": [{"id": "x", "status": "sent"}]}, "id not a uuid"),
    ({"lease_id": LEASE, "results": [{"id": ITEM, "status": "skipped"}]}, "status the bot may not report"),
    ({"lease_id": LEASE, "results": [{"id": ITEM, "status": "failed", "error": "Bad Request"}]}, "error is text"),
]


@pytest.mark.parametrize(("body", "why"), INVALID_ACKS, ids=[why for _, why in INVALID_ACKS])
async def test_an_ack_with_invalid_input_is_422(scene: Scene, body: dict[str, Any], why: str) -> None:
    assert (await scene.bot.post(OUTBOX_ACK_URL, json=body)).status_code == 422


async def test_an_ack_of_more_than_a_hundred_results_is_422(scene: Scene) -> None:
    lease = "00000000-0000-4000-8000-000000000000"
    results = [ack_result(f"00000000-0000-4000-8000-{index:012d}") for index in range(101)]

    assert (await http_ack(scene.bot, lease, results)).status_code == 422
    assert (await http_ack(scene.bot, lease, results[:100])).status_code == 200


async def test_an_ack_names_what_it_applied_and_what_it_ignored(scene: Scene, session: AsyncSession) -> None:
    await add_people(session, 3, first_telegram_id=FIRST_TELEGRAM_ID)
    await admin_create_broadcast(scene.api)
    claimed = (await http_claim(scene.bot)).json()
    ids = [item["id"] for item in claimed["items"]]

    foreign = await http_ack(scene.bot, "00000000-0000-4000-8000-000000000000", [ack_result(ids[0])])
    mine = await http_ack(scene.bot, claimed["lease_id"], [ack_result(ids[0]), ack_result(ids[1])])
    again = await http_ack(scene.bot, claimed["lease_id"], [ack_result(ids[0]), ack_result(ids[1])])

    assert foreign.status_code == 200 and foreign.json() == {"applied": [], "ignored": [ids[0]]}
    assert sorted(mine.json()["applied"]) == sorted(ids[:2]) and mine.json()["ignored"] == []
    assert again.status_code == 200 and again.json()["applied"] == []  # повтор ничего не меняет
    assert (await scene.api.get(BROADCASTS_URL)).json()[0]["sent"] == 2


async def test_a_person_deleted_in_the_middle_drops_out_of_the_broadcast_without_breaking_it(
    scene: Scene, session: AsyncSession
) -> None:
    people = await add_people(session, 5, first_telegram_id=FIRST_TELEGRAM_ID + 1)
    created = await admin_create_broadcast(scene.api)
    assert (await scene.api.delete(f"/api/v1/vpn-users/{people[0].id}")).status_code == 204

    delivered, _ = await run_the_bot(scene)

    done = (await scene.api.get(broadcast_url(created["id"]))).json()
    assert len(delivered) == 4 and done["total"] == 5 and done["sent"] + done["blocked"] + done["failed"] == 4
    assert done["state"] == "finished"


async def test_cancelling_in_the_middle_stops_the_rest_and_the_leased_batch_still_arrives(
    scene: Scene, session: AsyncSession
) -> None:
    await add_people(session, 250, first_telegram_id=FIRST_TELEGRAM_ID + 1)
    created = await admin_create_broadcast(scene.api)
    claimed = await scene.client.claim(BATCH)

    cancelled = (await scene.api.post(broadcast_url(created["id"], "/cancel"))).json()
    await scene.client.ack(claimed.lease_id, [outbox_result(item.id, "sent") for item in claimed.items])
    after = await scene.client.claim(BATCH)

    assert cancelled["state"] == "cancelled" and cancelled["cancelled"] == 150
    assert after.items == []
    done = (await scene.api.get(broadcast_url(created["id"]))).json()
    assert (done["sent"], done["cancelled"], done["total"]) == (100, 150, 250)
    assert done["state"] == "cancelled" and done["finished_at"] is not None


# --- ссылки на конфиг ----------------------------------------------------------------------------------------------


async def test_a_config_broadcast_gives_every_person_a_new_working_link_and_kills_the_old_one(scene: Scene) -> None:
    ids = await activate(scene, 3)
    old = await scene.client.config_link(TELEGRAM_ID)
    assert old is not None

    created = await admin_create_broadcast(
        scene.api, kind=CONFIG, target=TARGET_PROTOCOL, protocol_id=scene.protocol_id
    )
    claim = await scene.client.claim(BATCH)

    assert created["total"] == 3 and len(claim.items) == 3 and {item.kind for item in claim.items} == {"config"}
    assert {item.telegram_id for item in claim.items} == {TELEGRAM_ID + number for number in range(3)}
    urls = [item.config_url for item in claim.items]
    assert len(set(urls)) == 3 and all(url.startswith(f"{PUBLIC_BASE}/c#") for url in urls)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as person:
        opened = [await person.post(PUBLIC_CONFIG_URL, json={"token": token_of(url)}) for url in urls]
        previous = await person.post(PUBLIC_CONFIG_URL, json={"token": token_of(old.config_url)})
    assert [response.status_code for response in opened] == [200, 200, 200] and previous.status_code == 404
    assert all(item.valid_until for item in claim.items) and ids


async def test_the_answer_for_a_config_item_has_the_agreed_shape(scene: Scene) -> None:
    await activate(scene, 1)
    await admin_create_broadcast(scene.api, kind=CONFIG, target=TARGET_PROTOCOL, protocol_id=scene.protocol_id)

    body = (await http_claim(scene.bot)).json()

    (item,) = body["items"]
    assert set(item) == {"id", "kind", "telegram_id", "config_url", "valid_until"} and item["kind"] == "config"
    assert body["texts"] == {}  # у ссылки на конфиг текста рассылки нет: сообщение собирает бот


async def test_the_token_of_a_link_is_nowhere_in_the_database_the_audit_or_the_logs(
    scene: Scene, caplog: pytest.LogCaptureFixture
) -> None:
    await activate(scene, 2)
    caplog.set_level(logging.DEBUG)
    created = await admin_create_broadcast(
        scene.api, kind=CONFIG, target=TARGET_PROTOCOL, protocol_id=scene.protocol_id
    )

    claim = await scene.client.claim(BATCH)
    await scene.client.ack(claim.lease_id, [outbox_result(item.id, "sent") for item in claim.items])

    dump = await tables_dump("bot_outbox", "bot_broadcasts", "audit_logs", "jobs")
    for item in claim.items:
        token = token_of(item.config_url)
        assert token not in dump and item.config_url not in dump
        assert token not in caplog.text and token not in str([vars(record) for record in caplog.records])
        assert hash_token(token) in str(await sql_rows("SELECT token_hash FROM config_links"))
    assert created["id"] in dump


async def test_a_link_for_one_person_goes_through_the_same_queue_and_comes_with_a_working_page(
    scene: Scene,
) -> None:
    (user_id,) = await activate(scene, 1)
    queued = (await scene.api.post(user_config_link_url(user_id))).json()

    claim = await scene.client.claim(BATCH)

    (item,) = claim.items
    assert item.id == queued["outbox_id"] and item.kind == "config" and item.broadcast_id is None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as person:
        assert (await person.post(PUBLIC_CONFIG_URL, json={"token": token_of(item.config_url)})).status_code == 200


# --- снятие пометки «заблокировал бота» ----------------------------------------------------------------------------


async def block_the_bot(scene: Scene, user_id: str) -> None:
    created = await admin_create_broadcast(scene.api, target=TARGET_SELECTED, user_ids=[user_id])
    claim = await scene.client.claim(BATCH)
    await scene.client.ack(claim.lease_id, [outbox_result(item.id, "blocked", "blocked") for item in claim.items])
    assert created["total"] == 1
    assert (await scene.api.get(f"/api/v1/vpn-users/{user_id}")).json()["bot_blocked_at"] is not None


async def test_a_person_who_blocked_the_bot_is_unmarked_when_the_bot_asks_for_their_status(scene: Scene) -> None:
    (user_id,) = await activate(scene, 1)
    await block_the_bot(scene, user_id)

    assert (await scene.client.user_by_telegram(TELEGRAM_ID)) is not None

    assert (await scene.api.get(f"/api/v1/vpn-users/{user_id}")).json()["bot_blocked_at"] is None


async def test_a_person_who_blocked_the_bot_is_unmarked_when_the_bot_asks_for_a_new_link(scene: Scene) -> None:
    (user_id,) = await activate(scene, 1)
    await block_the_bot(scene, user_id)

    assert (await scene.client.config_link(TELEGRAM_ID)) is not None

    assert (await scene.api.get(f"/api/v1/vpn-users/{user_id}")).json()["bot_blocked_at"] is None


async def test_the_admin_cannot_send_a_link_to_a_blocker_until_they_write_to_the_bot_again(scene: Scene) -> None:
    (user_id,) = await activate(scene, 1)
    await block_the_bot(scene, user_id)

    refused = await scene.api.post(user_config_link_url(user_id))
    await scene.client.user_by_telegram(TELEGRAM_ID)  # человек написал боту /start или /status
    allowed = await scene.api.post(user_config_link_url(user_id))

    assert refused.status_code == 409 and refused.json()["reason"] == "bot_blocked"
    assert allowed.status_code == 202


async def test_the_status_request_of_a_person_who_never_blocked_the_bot_changes_nothing(scene: Scene) -> None:
    (user_id,) = await activate(scene, 1)

    await scene.client.user_by_telegram(TELEGRAM_ID)

    assert (await scene.api.get(f"/api/v1/vpn-users/{user_id}")).json()["bot_blocked_at"] is None


# --- аудит всего сценария ------------------------------------------------------------------------------------------


async def test_the_audit_of_the_whole_flow_has_no_text_no_telegram_ids_no_addresses_no_people_lists(
    scene: Scene, session: AsyncSession
) -> None:
    people = await add_people(session, 20, first_telegram_id=FIRST_TELEGRAM_ID)
    ids = await activate(scene, 2)
    await admin_create_broadcast(scene.api)
    await admin_create_broadcast(scene.api, kind=CONFIG, target=TARGET_SELECTED, user_ids=ids)
    await scene.api.post(user_config_link_url(ids[0]))
    delivered, _ = await run_the_bot(scene)

    dump = await audit_dump()

    assert delivered
    assert NEWS_MARKER not in dump and "/c#" not in dump and PUBLIC_BASE not in dump
    for person in people:
        assert str(person.telegram_id) not in dump and str(person.id) not in dump
    for number in range(2):
        assert str(TELEGRAM_ID + number) not in dump
    assert "bot_broadcast_created" in dump and "bot_config_link_queued" in dump and "config_link_created" in dump
