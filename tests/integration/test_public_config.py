"""Страница конфига (этап 25.3) через HTTP: `POST /api/v1/bot/users/by-telegram/{id}/config-link` выпускает ссылку,
`POST /api/v1/public/config {token}` открывает её без входа.

Главное: любой отказ при открытии - один и тот же 404 побайтно (по ответу не видно причину), токена нет ни в логах,
ни в аудите, ни в БД открытым текстом, лимиты считают неудачи по IP, а не открытия.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from core.database.postgres import session_factory
from core.security import hash_token
from infrastructure.workers.worker import JobWorker
from main import app
from modules.bot.models.registration import BotRegistration
from tests.bot_support import (
    CONFIG_UNAVAILABLE,
    NO_ACCESS,
    NOT_CONFIGURED,
    NOT_FOUND_BODY,
    NOT_READY,
    PUBLIC_BASE,
    PUBLIC_CONFIG_URL,
    SECOND_EMAIL,
    SECOND_TELEGRAM_ID,
    TELEGRAM_ID,
    TOKEN_SHAPE,
    accepted,
    admin_create_invite,
    admin_create_key,
    audit_dump,
    bot_client,
    fetch,
    http_activate,
    links_in_db,
    live_links_in_db,
    open_over_http,
    registration_body,
    request_link,
    set_public_address,
    token_of,
)
from tests.fakes.fake_ssh import FakeServer
from tests.fixtures import World

PAGE_FIELDS = {"name", "valid_until", "server_name", "config", "share_link", "share_qr", "file_name"}
NOT_FOUND_BYTES = b'{"detail":"Not found"}'


@dataclass
class Scene:
    protocol_id: UUID
    fake: FakeServer
    api: httpx.AsyncClient
    bot: httpx.AsyncClient
    worker: JobWorker


@pytest.fixture
async def scene(api: httpx.AsyncClient, world: World, worker: JobWorker) -> AsyncIterator[Scene]:
    _, protocol, fake = await world.server()
    assert protocol is not None
    key = (await admin_create_key(api))["key"]
    async with bot_client(key) as bot:
        yield Scene(protocol.id, fake, api, bot, worker)


@asynccontextmanager
async def visitor(ip: str = "198.51.100.7") -> AsyncIterator[httpx.AsyncClient]:
    """Человек с телефона: ни JWT, ни ключа бота, ни cookie; адрес задаётся (лимиты считаются по нему)."""
    transport = httpx.ASGITransport(app=app, client=(ip, 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def active_person(scene: Scene, **fields: Any) -> dict[str, Any]:
    return await http_activate(scene.api, scene.bot, scene.protocol_id, scene.worker, **fields)


async def new_link(scene: Scene, telegram_id: int = TELEGRAM_ID) -> tuple[str, dict[str, Any]]:
    response = await request_link(scene.bot, telegram_id)
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return token_of(body["config_url"]), body


async def grant_of(telegram_id: int = TELEGRAM_ID) -> str:
    async with session_factory() as session:
        return str(
            (
                await session.execute(
                    text(
                        "SELECT g.id FROM access_grants g JOIN subscriptions s ON s.id = g.subscription_id "
                        "JOIN vpn_users u ON u.id = s.user_id WHERE u.telegram_id = :t"
                    ),
                    {"t": telegram_id},
                )
            ).scalar_one()
        )


async def sql(statement: str, **params: Any) -> None:
    async with session_factory() as session:
        await session.execute(text(statement), params)
        await session.commit()


# --- выпуск ссылки ботом -------------------------------------------------------------------------------------------


async def test_the_bot_gets_a_link_with_the_token_in_the_fragment_and_the_end_date(scene: Scene) -> None:
    await active_person(scene)

    response = await request_link(scene.bot)

    assert response.status_code in (200, 201), response.text
    body = response.json()
    assert set(body) == {"config_url", "valid_until"}
    assert body["config_url"].startswith(f"{PUBLIC_BASE}/c#") and TOKEN_SHAPE.fullmatch(token_of(body["config_url"]))
    until = datetime.fromisoformat(body["valid_until"])
    assert abs(until - (datetime.now(timezone.utc) + timedelta(days=30))) < timedelta(minutes=5)


async def test_the_token_is_not_in_the_database_in_the_clear(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)

    (row,) = await links_in_db()
    assert row.token_hash == hash_token(token)
    async with session_factory() as session:
        dumps = [
            str((await session.execute(text(f"SELECT * FROM {table}"))).all())
            for table in ("config_links", "audit_logs", "jobs", "vpn_credentials")
        ]
    assert all(token not in dump for dump in dumps)


async def test_a_new_link_closes_the_previous_one(scene: Scene) -> None:
    await active_person(scene)
    first, _ = await new_link(scene)
    second, _ = await new_link(scene)

    async with visitor() as person:
        assert (await open_over_http(person, first)).status_code == 404
        assert (await open_over_http(person, second)).status_code == 200
    assert len(await live_links_in_db()) == 1


async def test_a_telegram_without_an_account_is_404(scene: Scene) -> None:
    response = await request_link(scene.bot, 5559990001)
    assert response.status_code == 404 and response.json() != {"detail": "Not Found"}  # не «нет маршрута»
    assert await links_in_db() == []


@pytest.mark.parametrize("telegram_id", ["abc", "0", "-5"])
async def test_a_telegram_id_out_of_range_is_422(scene: Scene, telegram_id: str) -> None:
    assert (await scene.bot.post(f"/api/v1/bot/users/by-telegram/{telegram_id}/config-link")).status_code == 422


async def test_a_person_whose_client_is_not_issued_yet_gets_409_not_ready(scene: Scene) -> None:
    invite = await admin_create_invite(scene.api, scene.protocol_id)
    response = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"]))
    assert accepted(response)  # воркер не запускался: клиента на сервере ещё нет

    refusal = await request_link(scene.bot)

    assert refusal.status_code == 409 and refusal.json()["reason"] == NOT_READY
    assert await links_in_db() == []


async def test_revoked_access_gives_409_no_access_and_no_link(scene: Scene) -> None:
    await active_person(scene)
    assert (await scene.api.post(f"/api/v1/access/{await grant_of()}/revoke")).status_code == 200

    refusal = await request_link(scene.bot)

    assert refusal.status_code == 409 and refusal.json()["reason"] == NO_ACCESS
    assert await links_in_db() == []


async def test_a_person_with_only_an_imported_client_gets_409_config_unavailable(scene: Scene) -> None:
    await active_person(scene)
    await sql("UPDATE vpn_credentials SET source = 'IMPORTED', encrypted_config = NULL")

    refusal = await request_link(scene.bot)

    assert refusal.status_code == 409 and refusal.json()["reason"] == CONFIG_UNAVAILABLE
    assert await links_in_db() == []


async def test_without_a_public_address_the_answer_is_409_not_configured_with_a_warning(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await active_person(scene)
    set_public_address(monkeypatch, "", domain="")

    with caplog.at_level(logging.WARNING):
        refusal = await request_link(scene.bot)

    assert refusal.status_code == 409 and refusal.json()["reason"] == NOT_CONFIGURED
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert await live_links_in_db() == []


async def test_the_address_may_come_from_the_domain_and_the_port(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> None:
    await active_person(scene)
    set_public_address(monkeypatch, "", domain="vpn.example.org", port=8443)

    response = await request_link(scene.bot)

    assert response.status_code in (200, 201)
    assert response.json()["config_url"].startswith("https://vpn.example.org:8443/c#")


async def test_issuing_a_link_leaves_one_audit_event_without_the_token(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)

    dump = await audit_dump()
    assert dump.count("config_link_created") == 1
    assert token not in dump and hash_token(token) not in dump and str(TELEGRAM_ID) not in dump


# --- открытие: что видит человек -----------------------------------------------------------------------------------


async def test_a_person_opens_the_page_without_any_credentials(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)

    async with visitor() as person:
        response = await open_over_http(person, token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == PAGE_FIELDS
    assert body["name"] == "Иван Петров" and body["server_name"]
    assert body["config"].lstrip().startswith("[Interface]")
    assert body["share_link"].startswith("vpn://") and body["share_qr"] and all(body["share_qr"])
    assert body["file_name"] and all(ch.isalnum() or ch in "_-" for ch in body["file_name"])
    assert abs(datetime.fromisoformat(body["valid_until"]) - (datetime.now(timezone.utc) + timedelta(days=30))) < (
        timedelta(minutes=5)
    )


async def test_the_response_is_not_cacheable_and_not_indexable_and_sets_no_cookie(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)

    async with visitor() as person:
        response = await open_over_http(person, token)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert "set-cookie" not in response.headers


async def test_the_token_in_the_url_or_the_query_opens_nothing(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)

    async with visitor() as person:
        in_path = await person.get(f"{PUBLIC_CONFIG_URL}/{token}")
        as_get = await person.get(PUBLIC_CONFIG_URL, params={"token": token})
        no_body = await person.post(PUBLIC_CONFIG_URL)

    assert in_path.status_code in (404, 405) and as_get.status_code in (404, 405)
    assert no_body.status_code == 422
    assert "[Interface]" not in in_path.text + as_get.text + no_body.text


async def test_opening_notes_the_time_and_writes_nothing_to_the_audit(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)
    before = await audit_dump()

    async with visitor() as person:
        assert (await open_over_http(person, token)).status_code == 200
        assert (await open_over_http(person, token)).status_code == 200

    (row,) = await links_in_db()
    assert row.last_opened_at is not None
    assert await audit_dump() == before


async def test_a_page_of_one_person_never_shows_the_config_of_another(scene: Scene) -> None:
    await active_person(scene)
    await active_person(scene, telegram_id=SECOND_TELEGRAM_ID, email=SECOND_EMAIL, name="Мария")
    mine, _ = await new_link(scene, TELEGRAM_ID)
    hers, _ = await new_link(scene, SECOND_TELEGRAM_ID)

    async with visitor() as person:
        first, second = await open_over_http(person, mine), await open_over_http(person, hers)

    assert first.json()["name"] == "Иван Петров" and second.json()["name"] == "Мария"
    assert first.json()["config"] != second.json()["config"]


# --- открытие: отказ всегда один и тот же --------------------------------------------------------------------------


async def refusals_by_reason(scene: Scene) -> dict[str, httpx.Response]:
    """Один и тот же человек, одна и та же ссылка - и каждый раз другая причина отказа (на свежем окружении)."""
    results: dict[str, httpx.Response] = {}
    async with visitor("203.0.113.1") as person:
        results["unknown token"] = await open_over_http(person, "Z" * 43)
        for name, token in {
            "empty": "",
            "short": "abc",
            "long": "x" * 44,
            "bad characters": "!" * 43,
            "unicode": "я" * 43,
        }.items():
            results[f"malformed: {name}"] = await open_over_http(person, token)
    return results


async def test_every_reason_for_refusal_gives_the_same_404_byte_for_byte(scene: Scene) -> None:
    await active_person(scene)
    first, _ = await new_link(scene)
    second, _ = await new_link(scene)  # первая заменена
    results = await refusals_by_reason(scene)
    async with visitor("203.0.113.2") as person:
        results["replaced"] = await open_over_http(person, first)

    # доступ закончился разными способами: каждый раз ссылка исправна до этого момента и мертва после
    grant_id = await grant_of()
    await sql("UPDATE subscriptions SET valid_until = now() - interval '1 second'")
    async with visitor("203.0.113.3") as person:
        results["period ended"] = await open_over_http(person, second)
    await sql("UPDATE subscriptions SET valid_until = now() + interval '30 days'")
    async with visitor("203.0.113.4") as person:
        assert (await open_over_http(person, second)).status_code == 200  # снова жива: проверка при каждом открытии
    assert (await scene.api.post(f"/api/v1/access/{grant_id}/revoke")).status_code == 200
    async with visitor("203.0.113.5") as person:
        results["access revoked"] = await open_over_http(person, second)  # задача REVOKE_ACCESS ещё не выполнена
    await scene.worker.process_next(timeout=1)
    async with visitor("203.0.113.6") as person:
        results["client removed"] = await open_over_http(person, second)

    assert len(results) >= 9
    assert {name: response.status_code for name, response in results.items()} == dict.fromkeys(results, 404)
    assert {response.content for response in results.values()} == {NOT_FOUND_BYTES}
    assert {response.headers["content-type"] for response in results.values()} == {"application/json"}
    assert all("set-cookie" not in response.headers for response in results.values())
    assert all(response.json() == NOT_FOUND_BODY for response in results.values())


async def test_an_imported_client_looks_like_any_other_refusal(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)
    await sql("UPDATE vpn_credentials SET source = 'IMPORTED', encrypted_config = NULL")

    async with visitor() as person:
        response = await open_over_http(person, token)

    assert response.status_code == 404 and response.content == NOT_FOUND_BYTES


async def test_a_deleted_client_looks_like_any_other_refusal(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)
    await sql("DELETE FROM vpn_credentials")

    async with visitor() as person:
        response = await open_over_http(person, token)

    assert response.status_code == 404 and response.content == NOT_FOUND_BYTES


async def test_a_refusal_carries_no_store_and_no_robots_headers_too(scene: Scene) -> None:
    async with visitor() as person:
        response = await open_over_http(person, "Z" * 43)

    assert response.headers["cache-control"] == "no-store" and "noindex" in response.headers["x-robots-tag"]


# --- лимиты --------------------------------------------------------------------------------------------------------

PROBE = 60  # больше любого разумного порога неудач; упереться в потолок значит, что лимита нет


async def test_failures_from_one_address_end_in_429_with_retry_after_and_another_address_still_opens(
    scene: Scene,
) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)

    async with visitor("192.0.2.10") as guesser:
        limited = None
        for _ in range(PROBE):
            response = await open_over_http(guesser, "Z" * 43)
            if response.status_code == 429:
                limited = response
                break
            assert response.status_code == 404
        assert limited is not None, "failed opens must be limited per address"
        assert int(limited.headers["retry-after"]) >= 1
        assert "set-cookie" not in limited.headers
        assert (await open_over_http(guesser, token)).status_code == 429  # лимит проверяется до поиска ссылки
    async with visitor("192.0.2.11") as other:
        assert (await open_over_http(other, token)).status_code == 200  # чужой адрес не наказан


async def test_successful_opens_do_not_count_as_failures(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)

    async with visitor("192.0.2.20") as person:
        statuses = {(await open_over_http(person, token)).status_code for _ in range(30)}
        assert statuses == {200}
        assert (await open_over_http(person, "Z" * 43)).status_code == 404  # и неудач за ними не накопилось


async def test_too_many_requests_from_one_address_end_in_429_even_if_all_are_valid(scene: Scene) -> None:
    await active_person(scene)
    token, _ = await new_link(scene)

    async with visitor("192.0.2.30") as person:
        limited = None
        for _ in range(200):
            response = await open_over_http(person, token)
            if response.status_code == 429:
                limited = response
                break
        assert limited is not None, "requests must be limited per address"
        assert int(limited.headers["retry-after"]) >= 1


# --- секреты -------------------------------------------------------------------------------------------------------


async def test_the_token_is_nowhere_in_the_logs_of_the_whole_conversation(
    scene: Scene, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await active_person(scene)
        token, body = await new_link(scene)
        async with visitor("192.0.2.40") as person:
            assert (await open_over_http(person, token)).status_code == 200
            await open_over_http(person, "Z" * 43)
            await open_over_http(person, "bad")
        replaced, _ = await new_link(scene)
        async with visitor("192.0.2.41") as person:
            assert (await open_over_http(person, token)).status_code == 404

    logs = "\n".join(str(vars(record)) + record.getMessage() for record in caplog.records)
    assert logs, "the conversation must leave some trace, otherwise the check below proves nothing"
    for secret in (token, replaced, hash_token(token), body["config_url"]):
        assert secret not in logs
    page_config_marker = "PrivateKey"
    assert page_config_marker not in logs


async def test_the_token_is_not_visible_to_an_admin_in_the_registrations_or_the_audit(
    scene: Scene,
) -> None:
    """Токена нет и в том, что видит админ: список регистраций и аудит."""
    started = await active_person(scene)
    token, _ = await new_link(scene)

    listed = await scene.api.get("/api/v1/bot-admin/registrations")
    audit = await scene.api.get("/api/v1/audit-logs")

    assert token not in listed.text and token not in audit.text
    registration = await fetch(BotRegistration, UUID(started["registration_id"]))
    assert registration is not None and token not in str(vars(registration))
