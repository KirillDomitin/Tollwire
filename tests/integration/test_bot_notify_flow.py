"""Сквозной сценарий уведомлений (этап 25.5): health -> воркер -> `server_health_changed` -> сборщик `bot_notify` ->
`claim(kinds)` через настоящий `PanelClient` -> `ack`.

Главное (готовность этапа): при падении сервера приходит ровно одно сообщение, повторная проверка того же состояния
молчит, при восстановлении приходит ровно одно сообщение. Бот старой версии (без `kinds`) уведомлений не видит."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from infrastructure.workers.scheduler import schedule_health_checks
from infrastructure.workers.worker import JobWorker
from main import app
from tests.bot_client_support import PanelClient, outbox_result
from tests.bot_notify_support import (
    HEALTH_CHANGED,
    SERVER_DOWN,
    SERVER_RECOVERED,
    TG_ADMIN,
    audit_rows,
    collect,
    http_link_admin,
    notify_rows,
)
from tests.bot_support import admin_create_key, bot_client
from tests.fakes.fake_ssh import FakeServer
from tests.fixtures import World
from tests.integration.conftest import drain


@dataclass
class Scene:
    api: httpx.AsyncClient
    bot: httpx.AsyncClient
    client: PanelClient
    worker: JobWorker
    server: Any
    fake: FakeServer

    async def check(self) -> None:
        """Плановая проверка здоровья всех серверов, воркер её исполняет, сборщик делает свой проход."""
        await schedule_health_checks()
        await drain(self.worker)
        await collect()

    async def notifications(self) -> list[Any]:
        claim = await self.client.claim(50, kinds=["notify"])
        results = [outbox_result(item.id, "sent") for item in claim.items]
        if results:
            await self.client.ack(claim.lease_id, results)
        return list(claim.items)


@pytest.fixture
async def scene(api: httpx.AsyncClient, world: World, worker: JobWorker) -> AsyncIterator[Scene]:
    server, _, fake = await world.server()
    key = await admin_create_key(api)
    client = PanelClient("http://testserver", key["key"], transport=httpx.ASGITransport(app=app))
    async with bot_client(key["key"]) as bot:
        await http_link_admin(api, bot, TG_ADMIN)
        await collect()  # первый проход ставит курсор «сейчас»: прошлого у сборщика нет
        try:
            yield Scene(api, bot, client, worker, server, fake)
        finally:
            await client.aclose()


async def test_a_server_going_down_gives_exactly_one_message(scene: Scene) -> None:
    scene.fake.reachable = False

    await scene.check()

    (item,) = await scene.notifications()
    assert item.kind == "notify" and item.telegram_id == TG_ADMIN and item.format == "plain"
    assert scene.server.name in item.text


async def test_a_repeated_down_check_is_silent(scene: Scene) -> None:
    scene.fake.reachable = False
    await scene.check()
    assert len(await scene.notifications()) == 1

    await scene.check()
    await scene.check()

    assert await scene.notifications() == []
    assert len(await audit_rows(HEALTH_CHANGED)) == 1


async def test_recovery_gives_exactly_one_more_message(scene: Scene) -> None:
    scene.fake.reachable = False
    await scene.check()
    await scene.notifications()
    scene.fake.reachable = True

    await scene.check()
    await scene.check()

    (item,) = await scene.notifications()
    assert scene.server.name in item.text
    assert [row.notify_key for row in await notify_rows()] == [SERVER_DOWN, SERVER_RECOVERED]


async def test_a_healthy_server_that_stays_healthy_gives_nothing(scene: Scene) -> None:
    await scene.check()
    await scene.check()

    assert await scene.notifications() == []


async def test_a_bot_without_kinds_never_sees_notify(scene: Scene) -> None:
    scene.fake.reachable = False
    await scene.check()

    claim = await scene.client.claim(50)

    assert claim.items == []
    assert len(await notify_rows(status="pending")) == 1


async def test_the_manual_check_of_an_admin_notifies_too_and_is_attributed_to_him(scene: Scene) -> None:
    scene.fake.reachable = False

    response = await scene.api.post(f"/api/v1/servers/{scene.server.id}/health")
    assert response.status_code == 202
    await drain(scene.worker)
    await collect()

    assert len(await scene.notifications()) == 1
    (row,) = await audit_rows(HEALTH_CHANGED)
    assert row[0] is not None  # ручная проверка идёт от имени админа, плановая — от системы


async def test_a_scheduled_check_is_attributed_to_the_system(scene: Scene) -> None:
    scene.fake.reachable = False

    await scene.check()

    (row,) = await audit_rows(HEALTH_CHANGED)
    assert row[0] is None


async def test_unlinking_stops_the_messages_and_drops_the_queue(scene: Scene) -> None:
    scene.fake.reachable = False
    await scene.check()

    assert (await scene.api.delete("/api/v1/bot-admin/me/telegram")).status_code == 204

    assert await scene.notifications() == []
    assert await notify_rows() == []
    scene.fake.reachable = True
    await scene.check()
    assert await notify_rows() == []
