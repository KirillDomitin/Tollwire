"""Бот (этап 25.1) через HTTP: ключ -> приглашение -> POST /registrations -> воркер на FakeServer ->
active -> users/by-telegram.

Админский API (`/api/v1/bot-admin/*`) и Bot API (`/api/v1/bot/*`, X-Bot-Key) - настоящие, вход настоящий, SSH фейковый.
Коды ответов, которые план не фиксирует (201 или 200 на POST /registrations), проверяются через `accepted()`.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from infrastructure.workers.worker import JobWorker
from modules.auth.models.admin import AdminRole
from modules.bot.models.invite import BotInvite
from modules.bot.models.registration import BotRegistration
from modules.jobs.models.job import Job, JobType
from tests.bot_support import (
    EMAIL,
    INVITES_URL,
    KEYS_URL,
    NO_ACCESS,
    PUBLIC_BASE,
    PUBLIC_CONFIG_URL,
    REGISTRATIONS_URL,
    REVOKED,
    SECOND_EMAIL,
    SECOND_TELEGRAM_ID,
    TELEGRAM_ID,
    UNKNOWN,
    USED,
    VALID,
    accepted,
    admin_create_invite,
    admin_create_key,
    audit_dump,
    bot_audit_dump,
    bot_client,
    fetch,
    new_uuid,
    registration_body,
    request_link,
    token_of,
)
from tests.fakes.fake_ssh import CONFIG_FILE, FakeServer
from tests.fixtures import World
from tests.integration.conftest import drain, login_client, make_admin
from tests.integration.test_gateway_assignments_api import assignments, gateway_with_exits


@dataclass
class Scene:
    protocol_id: UUID
    fake: FakeServer
    key: str
    bot: httpx.AsyncClient


@pytest.fixture
async def scene(api: httpx.AsyncClient, world: World) -> AsyncIterator[Scene]:
    """Сервер с AmneziaWG на фейке, ключ бота (выпущен админом через API) и клиент бота с этим ключом."""
    _, protocol, fake = await world.server()
    assert protocol is not None
    key = (await admin_create_key(api))["key"]
    async with bot_client(key) as bot:
        yield Scene(protocol.id, fake, key, bot)


async def check(bot: httpx.AsyncClient, code: str) -> str:
    response = await bot.post("/api/v1/bot/invites/check", json={"code": code})
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


async def start_registration(scene: Scene, api: httpx.AsyncClient, **fields: Any) -> tuple[dict[str, Any], str]:
    """Приглашение от админа и POST /registrations от бота; возвращает тело ответа и код приглашения."""
    invite = await admin_create_invite(api, scene.protocol_id)
    response = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"], **fields))
    assert accepted(response), response.text
    return response.json(), str(invite["code"])


async def registration_status(scene: Scene, registration_id: str) -> dict[str, Any]:
    response = await scene.bot.get(f"/api/v1/bot/registrations/{registration_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def admin_registration(api: httpx.AsyncClient, registration_id: str) -> dict[str, Any]:
    response = await api.get(REGISTRATIONS_URL)
    assert response.status_code == 200, response.text
    return next(item for item in response.json() if item["id"] == registration_id)


# --- сквозной сценарий ---------------------------------------------------------------------------------------------


async def test_invite_to_active_user_end_to_end(api: httpx.AsyncClient, worker: JobWorker, scene: Scene) -> None:
    invite = await admin_create_invite(api, scene.protocol_id, days=30)
    assert await check(scene.bot, invite["code"]) == VALID

    response = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"]))
    assert accepted(response), response.text
    started = response.json()
    assert started["status"] == "provisioning" and "config_url" not in started
    registration_id = started["registration_id"]
    assert (await registration_status(scene, registration_id))["status"] == "provisioning"

    assert await drain(worker) == 1  # одна задача CREATE_USER
    done = await registration_status(scene, registration_id)
    assert done["status"] == "active" and "config_url" not in done  # ссылку отдаёт только POST .../config-link

    assert len(scene.fake.interface_peers) == 1
    assert "grant-" in scene.fake.files[CONFIG_FILE]  # клиент записан в конфиг сервера
    assert await check(scene.bot, invite["code"]) == USED

    status = await scene.bot.get(f"/api/v1/bot/users/by-telegram/{TELEGRAM_ID}")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["name"] == "Иван Петров" and "config_url" not in body
    valid_until = datetime.fromisoformat(body["valid_until"])
    assert abs(valid_until - (datetime.now(timezone.utc) + timedelta(days=30))) < timedelta(minutes=5)


async def test_the_client_appears_in_the_panel_like_any_other(
    api: httpx.AsyncClient, worker: JobWorker, scene: Scene
) -> None:
    started, _ = await start_registration(scene, api)
    await drain(worker)
    registration = await fetch(BotRegistration, UUID(started["registration_id"]))
    credentials = await api.get(f"/api/v1/access/{registration.access_grant_id}/credentials")
    assert credentials.status_code == 200 and len(credentials.json()) == 1
    users = (await api.get("/api/v1/vpn-users")).json()
    (user,) = [item for item in users if item["id"] == str(registration.user_id)]
    assert user["access"] is not None and user["email"] == EMAIL


async def test_the_status_of_an_unregistered_telegram_is_404(scene: Scene) -> None:
    assert (await scene.bot.get(f"/api/v1/bot/users/by-telegram/{TELEGRAM_ID}")).status_code == 404


async def test_the_status_of_an_unknown_registration_is_404(scene: Scene) -> None:
    assert (await scene.bot.get(f"/api/v1/bot/registrations/{new_uuid()}")).status_code == 404


async def test_a_registration_id_that_is_not_a_uuid_is_422(scene: Scene) -> None:
    assert (await scene.bot.get("/api/v1/bot/registrations/not-a-uuid")).status_code == 422


async def test_a_telegram_id_that_is_not_a_number_is_422(scene: Scene) -> None:
    assert (await scene.bot.get("/api/v1/bot/users/by-telegram/abc")).status_code == 422


async def test_check_of_an_unknown_code_says_unknown_and_nothing_else(scene: Scene) -> None:
    response = await scene.bot.post("/api/v1/bot/invites/check", json={"code": "no-such-code-at-all"})
    assert response.status_code == 200 and response.json()["status"] == UNKNOWN


async def test_check_does_not_reveal_invite_details(api: httpx.AsyncClient, scene: Scene) -> None:
    invite = await admin_create_invite(api, scene.protocol_id, note="для Ивана")
    body = (await scene.bot.post("/api/v1/bot/invites/check", json={"code": invite["code"]})).json()
    assert not {"protocol_id", "days", "note", "expires_at", "id", "created_by"} & set(body)
    assert "для Ивана" not in str(body)


@pytest.mark.parametrize("code", ["", "x" * 65])
async def test_check_with_an_empty_or_oversized_code_is_422(scene: Scene, code: str) -> None:
    assert (await scene.bot.post("/api/v1/bot/invites/check", json={"code": code})).status_code == 422


# --- ссылка на страницу конфига (этап 25.3) ------------------------------------------------------------------------


async def test_registration_link_page_revoked_access_and_config_again_end_to_end(
    api: httpx.AsyncClient, worker: JobWorker, scene: Scene
) -> None:
    started, _ = await start_registration(scene, api)
    await drain(worker)
    assert (await registration_status(scene, started["registration_id"]))["status"] == "active"

    issued = await request_link(scene.bot)  # бот при active
    assert issued.status_code in (200, 201), issued.text
    assert issued.json()["config_url"].startswith(f"{PUBLIC_BASE}/c#")
    token = token_of(issued.json()["config_url"])

    async with bot_client() as person:  # человек открывает страницу: ни ключа, ни входа
        page = await person.post(PUBLIC_CONFIG_URL, json={"token": token})
        assert page.status_code == 200 and page.json()["name"] == "Иван Петров"
        assert page.json()["config"].lstrip().startswith("[Interface]")

        registration = await fetch(BotRegistration, UUID(started["registration_id"]))
        assert (await api.post(f"/api/v1/access/{registration.access_grant_id}/revoke")).status_code == 200
        closed = await person.post(PUBLIC_CONFIG_URL, json={"token": token})  # задача REVOKE_ACCESS ещё не выполнена
        assert closed.status_code == 404
        await drain(worker)
        assert (await person.post(PUBLIC_CONFIG_URL, json={"token": token})).status_code == 404

    again = await request_link(scene.bot)  # человек пишет /config
    assert again.status_code == 409 and again.json()["reason"] == NO_ACCESS


async def test_the_status_of_a_person_with_an_active_registration_tells_nothing_about_the_link(
    api: httpx.AsyncClient, worker: JobWorker, scene: Scene
) -> None:
    started, _ = await start_registration(scene, api)
    await drain(worker)
    await request_link(scene.bot)

    status = (await scene.bot.get(f"/api/v1/bot/users/by-telegram/{TELEGRAM_ID}")).json()
    registration = await registration_status(scene, started["registration_id"])

    assert "config_url" not in status and "config_url" not in registration
    assert "/c#" not in str(status) + str(registration)


# --- повтор и конфликты --------------------------------------------------------------------------------------------


async def test_posting_the_same_registration_twice_returns_the_same_one(
    api: httpx.AsyncClient, worker: JobWorker, scene: Scene
) -> None:
    started, code = await start_registration(scene, api)
    again = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(code))
    assert accepted(again) and again.json()["registration_id"] == started["registration_id"]
    assert await drain(worker) == 1  # задача одна, не две


async def test_another_telegram_with_a_used_invite_gets_409_without_echo(api: httpx.AsyncClient, scene: Scene) -> None:
    _, code = await start_registration(scene, api)
    response = await scene.bot.post(
        "/api/v1/bot/registrations",
        json=registration_body(code, telegram_id=SECOND_TELEGRAM_ID, email=SECOND_EMAIL, name="Мария"),
    )
    assert response.status_code == 409
    assert code not in response.text and SECOND_EMAIL not in response.text and "Мария" not in response.text


async def test_a_taken_email_is_409_and_the_invite_stays_usable(api: httpx.AsyncClient, scene: Scene) -> None:
    await api.post("/api/v1/vpn-users", json={"username": "old", "email": EMAIL})
    invite = await admin_create_invite(api, scene.protocol_id)
    response = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"]))
    assert response.status_code == 409 and EMAIL not in response.text
    assert await check(scene.bot, invite["code"]) == VALID  # приглашение не потрачено: можно с другой почтой
    retry = await scene.bot.post(
        "/api/v1/bot/registrations", json=registration_body(invite["code"], email=SECOND_EMAIL)
    )
    assert accepted(retry), retry.text


async def test_a_taken_telegram_id_is_409(api: httpx.AsyncClient, worker: JobWorker, scene: Scene) -> None:
    await start_registration(scene, api)
    await drain(worker)
    invite = await admin_create_invite(api, scene.protocol_id)
    response = await scene.bot.post(
        "/api/v1/bot/registrations", json=registration_body(invite["code"], email=SECOND_EMAIL)
    )
    assert response.status_code == 409
    assert await check(scene.bot, invite["code"]) == VALID


@pytest.mark.parametrize("code", ["no-such-code-at-all", "A" * 64])
async def test_an_unknown_invite_is_404_or_409(scene: Scene, code: str) -> None:
    response = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(code))
    assert response.status_code in (404, 409)


async def test_an_expired_invite_is_refused(api: httpx.AsyncClient, scene: Scene) -> None:
    invite = await admin_create_invite(api, scene.protocol_id)
    async with session_factory() as session:
        row = await session.get(BotInvite, UUID(invite["id"]))
        assert row is not None
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
    assert await check(scene.bot, invite["code"]) == "expired"
    response = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"]))
    assert response.status_code in (404, 409)


async def test_a_revoked_invite_is_refused(api: httpx.AsyncClient, scene: Scene) -> None:
    invite = await admin_create_invite(api, scene.protocol_id)
    assert (await api.post(f"{INVITES_URL}/{invite['id']}/revoke")).status_code == 200
    assert await check(scene.bot, invite["code"]) in (REVOKED, UNKNOWN)
    response = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"]))
    assert response.status_code in (404, 409)


# --- невалидный ввод -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "bad\x00name"),
        ("name", "line\nbreak"),
        ("name", ""),
        ("name", "x" * 65),
        ("email", "not-an-email"),
        ("email", "a" * 90 + "@example.org"),
        ("telegram_id", 0),
        ("telegram_id", -5),
        ("telegram_id", 2**63),
        ("telegram_username", "x" * 65),
    ],
)
async def test_invalid_registration_input_is_422_and_creates_nothing(
    api: httpx.AsyncClient, scene: Scene, field: str, value: Any
) -> None:
    invite = await admin_create_invite(api, scene.protocol_id)
    body = {**registration_body(invite["code"]), field: value}
    response = await scene.bot.post("/api/v1/bot/registrations", json=body)
    assert response.status_code == 422
    assert "not-an-email" not in response.text  # 422 не возвращает присланное значение
    assert await check(scene.bot, invite["code"]) == VALID
    assert (await api.get("/api/v1/vpn-users")).json() == []


async def test_missing_registration_fields_are_422(scene: Scene) -> None:
    assert (await scene.bot.post("/api/v1/bot/registrations", json={})).status_code == 422


# --- сбой выдачи, retry --------------------------------------------------------------------------------------------


async def test_failed_issue_then_admin_retry_ends_active(
    api: httpx.AsyncClient, worker: JobWorker, scene: Scene
) -> None:
    scene.fake.reachable = False
    started, _ = await start_registration(scene, api)
    registration_id = started["registration_id"]
    await drain(worker)
    assert (await registration_status(scene, registration_id))["status"] == "failed"
    listed = await admin_registration(api, registration_id)
    assert listed["status"] == "failed" and listed["error"]
    assert "PrivateKey" not in str(listed) and EMAIL not in str(listed["error"])

    scene.fake.reachable = True
    retried = await api.post(f"{REGISTRATIONS_URL}/{registration_id}/retry")
    assert retried.status_code in (200, 202), retried.text
    assert (await registration_status(scene, registration_id))["status"] == "provisioning"
    await drain(worker)
    assert (await registration_status(scene, registration_id))["status"] == "active"
    assert len(scene.fake.interface_peers) == 1
    assert (await admin_registration(api, registration_id))["status"] == "active"


async def test_a_second_retry_and_a_retry_of_an_active_registration_are_409(
    api: httpx.AsyncClient, worker: JobWorker, scene: Scene
) -> None:
    scene.fake.reachable = False
    started, _ = await start_registration(scene, api)
    registration_id = started["registration_id"]
    await drain(worker)
    scene.fake.reachable = True
    assert (await api.post(f"{REGISTRATIONS_URL}/{registration_id}/retry")).status_code in (200, 202)
    assert (await api.post(f"{REGISTRATIONS_URL}/{registration_id}/retry")).status_code == 409  # уже выдаётся
    await drain(worker)
    assert (await api.post(f"{REGISTRATIONS_URL}/{registration_id}/retry")).status_code == 409  # уже active


async def test_retry_after_the_access_was_revoked_is_409(
    api: httpx.AsyncClient, worker: JobWorker, scene: Scene
) -> None:
    scene.fake.reachable = False
    started, _ = await start_registration(scene, api)
    registration_id = started["registration_id"]
    await drain(worker)
    registration = await fetch(BotRegistration, UUID(registration_id))
    assert (await api.post(f"/api/v1/access/{registration.access_grant_id}/revoke")).status_code == 200
    scene.fake.reachable = True
    assert (await api.post(f"{REGISTRATIONS_URL}/{registration_id}/retry")).status_code == 409
    assert (await admin_registration(api, registration_id))["status"] == "failed"


async def test_retry_of_an_unknown_registration_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.post(f"{REGISTRATIONS_URL}/{new_uuid()}/retry")).status_code == 404


# --- шлюз ----------------------------------------------------------------------------------------------------------


async def test_a_gateway_invite_issues_a_gateway_client_with_an_exit(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    key = (await admin_create_key(api))["key"]
    invite = await admin_create_invite(api, protocol["id"])
    async with bot_client(key) as bot:
        response = await bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"]))
        assert accepted(response), response.text
        await drain(worker)
        body = (await bot.get(f"/api/v1/bot/registrations/{response.json()['registration_id']}")).json()
    assert body["status"] == "active"
    (assigned,) = await assignments(api, gw.sid)  # клиент шлюза получил выход, как при выдаче из панели
    assert assigned["slot"] in (1, 2)


# --- админский API: права, списки, секреты -------------------------------------------------------------------------


async def test_key_and_invite_lists_never_show_secrets(api: httpx.AsyncClient, scene: Scene) -> None:
    invite = await admin_create_invite(api, scene.protocol_id)
    keys = await api.get(KEYS_URL)
    invites = await api.get(INVITES_URL)
    assert keys.status_code == 200 and invites.status_code == 200
    assert scene.key not in keys.text and "key_hash" not in keys.text
    assert invite["code"] not in invites.text and "code_hash" not in invites.text


async def test_key_list_shows_what_the_bot_reported(api: httpx.AsyncClient, scene: Scene) -> None:
    heartbeat = await scene.bot.post("/api/v1/bot/heartbeat", json={"bot_username": "tollwire_bot", "version": "0.1.0"})
    assert heartbeat.status_code == 200 and heartbeat.json() == {}
    (item,) = (await api.get(KEYS_URL)).json()
    assert item["bot_username"] == "tollwire_bot" and item["last_seen_version"] == "0.1.0"
    assert item["last_seen_at"] is not None and item["revoked_at"] is None


async def test_invite_list_follows_the_invite_state(api: httpx.AsyncClient, worker: JobWorker, scene: Scene) -> None:
    waiting = await admin_create_invite(api, scene.protocol_id)
    used = await admin_create_invite(api, scene.protocol_id)
    revoked = await admin_create_invite(api, scene.protocol_id)
    assert (await api.post(f"{INVITES_URL}/{revoked['id']}/revoke")).status_code == 200
    response = await scene.bot.post("/api/v1/bot/registrations", json=registration_body(used["code"]))
    assert accepted(response)
    states = {item["id"]: item["status"] for item in (await api.get(INVITES_URL)).json()}
    assert states == {waiting["id"]: VALID, used["id"]: USED, revoked["id"]: REVOKED}


async def test_revoking_an_invite_twice_is_fine(api: httpx.AsyncClient, scene: Scene) -> None:
    invite = await admin_create_invite(api, scene.protocol_id)
    assert (await api.post(f"{INVITES_URL}/{invite['id']}/revoke")).status_code == 200
    assert (await api.post(f"{INVITES_URL}/{invite['id']}/revoke")).status_code == 200


async def test_revoking_unknown_things_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.post(f"{INVITES_URL}/{new_uuid()}/revoke")).status_code == 404
    assert (await api.post(f"{KEYS_URL}/{new_uuid()}/revoke")).status_code == 404


async def test_invite_for_an_unknown_protocol_is_404(api: httpx.AsyncClient) -> None:
    body = {
        "protocol_id": new_uuid(),
        "days": 30,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }
    assert (await api.post(INVITES_URL, json=body)).status_code == 404


@pytest.mark.parametrize(
    "override",
    [{"days": 0}, {"days": 3651}, {"days": "many"}, {"expires_at": "yesterday"}, {"protocol_id": "nope"}],
)
async def test_invalid_invite_is_422(api: httpx.AsyncClient, scene: Scene, override: dict[str, Any]) -> None:
    body = {
        "protocol_id": str(scene.protocol_id),
        "days": 30,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        **override,
    }
    assert (await api.post(INVITES_URL, json=body)).status_code == 422


async def test_invite_without_protocol_or_days_takes_them_from_the_settings(
    api: httpx.AsyncClient, scene: Scene
) -> None:
    """С 25.4 протокол, дни и срок необязательны: недостающее берётся из настроек бота (по умолчанию 30 и 7 дней);
    протокола нет ни в запросе, ни в настройках - 409 no_default_protocol."""
    deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    no_protocol = await api.post(INVITES_URL, json={"days": 30, "expires_at": deadline})
    assert no_protocol.status_code == 409 and no_protocol.json()["reason"] == "no_default_protocol"
    body = {"protocol_id": str(scene.protocol_id), "expires_at": deadline}
    created = await api.post(INVITES_URL, json=body)
    assert created.status_code == 201 and created.json()["days"] == 30


async def test_invite_that_expired_already_is_422(api: httpx.AsyncClient, scene: Scene) -> None:
    body = {
        "protocol_id": str(scene.protocol_id),
        "days": 30,
        "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }
    assert (await api.post(INVITES_URL, json=body)).status_code == 422


async def test_key_without_a_name_is_422(api: httpx.AsyncClient) -> None:
    assert (await api.post(KEYS_URL, json={})).status_code == 422
    assert (await api.post(KEYS_URL, json={"name": ""})).status_code == 422


async def test_a_viewer_reads_but_never_writes(session: AsyncSession, scene: Scene) -> None:
    async with login_client(await make_admin(session, AdminRole.VIEWER)) as viewer:
        assert (await viewer.get(INVITES_URL)).status_code == 200
        assert (await viewer.get(REGISTRATIONS_URL)).status_code == 200
        deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        invite = {"protocol_id": str(scene.protocol_id), "days": 30, "expires_at": deadline}
        assert (await viewer.post(INVITES_URL, json=invite)).status_code == 403
        assert (await viewer.post(f"{INVITES_URL}/{new_uuid()}/revoke")).status_code == 403
        assert (await viewer.post(f"{REGISTRATIONS_URL}/{new_uuid()}/retry")).status_code == 403
        assert (await viewer.post(KEYS_URL, json={"name": "x"})).status_code == 403
        assert (await viewer.post(f"{KEYS_URL}/{new_uuid()}/revoke")).status_code == 403


async def test_an_operator_manages_invites_but_not_keys(session: AsyncSession, scene: Scene) -> None:
    async with login_client(await make_admin(session, AdminRole.OPERATOR)) as operator:
        deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        invite = {"protocol_id": str(scene.protocol_id), "days": 30, "expires_at": deadline}
        created = await operator.post(INVITES_URL, json=invite)
        assert created.status_code == 201
        assert (await operator.post(f"{INVITES_URL}/{created.json()['id']}/revoke")).status_code == 200
        assert (await operator.post(f"{REGISTRATIONS_URL}/{new_uuid()}/retry")).status_code == 404  # право есть
        assert (await operator.post(KEYS_URL, json={"name": "x"})).status_code == 403
        assert (await operator.post(f"{KEYS_URL}/{new_uuid()}/revoke")).status_code == 403


async def test_only_a_superadmin_issues_and_revokes_keys(api: httpx.AsyncClient) -> None:
    created = await admin_create_key(api, "second-bot")
    assert (await api.post(f"{KEYS_URL}/{created['id']}/revoke")).status_code == 200


async def test_key_creation_response_shows_the_key_once(api: httpx.AsyncClient) -> None:
    created = await admin_create_key(api)
    assert created["key"].startswith(created["key_prefix"]) and len(created["key_prefix"]) == 8
    assert "key_hash" not in created
    listed = (await api.get(KEYS_URL)).json()
    assert created["key"] not in str(listed) and "key" not in listed[0]


# --- секреты: аудит, логи, задачи ---------------------------------------------------------------------------------


async def test_secrets_stay_out_of_audit_logs_and_job_params(
    api: httpx.AsyncClient, worker: JobWorker, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    _, protocol, _ = await world.server()
    assert protocol is not None
    with caplog.at_level(logging.DEBUG):
        key = (await admin_create_key(api))["key"]
        invite = await admin_create_invite(api, protocol.id)
        async with bot_client(key) as bot:
            await bot.post("/api/v1/bot/heartbeat", json={"bot_username": "tollwire_bot", "version": "0.1.0"})
            await bot.post("/api/v1/bot/invites/check", json={"code": invite["code"]})
            response = await bot.post("/api/v1/bot/registrations", json=registration_body(invite["code"]))
            assert accepted(response)
            await drain(worker)
            await bot.post("/api/v1/bot/registrations", json=registration_body("no-such-code-at-all"))

    secrets = (key, invite["code"])
    audit = await audit_dump()
    for secret in secrets:
        assert secret not in audit
    bot_events = await bot_audit_dump()
    assert "bot_key_created" in bot_events and "bot_invite_created" in bot_events
    assert "bot_registration_created" in bot_events and "bot_registration_activated" in bot_events
    for secret in (*secrets, EMAIL, EMAIL.split("@")[0], str(TELEGRAM_ID)):
        assert secret not in bot_events  # события бота: ни кода, ни ключа, ни почты, ни telegram_id

    logs = "\n".join(str(vars(record)) for record in caplog.records)
    for secret in secrets:
        assert secret not in logs
    async with session_factory() as session:
        for job in (await session.execute(select(Job).where(Job.type == JobType.CREATE_USER))).scalars():
            assert all(secret not in str(job.params) for secret in (*secrets, EMAIL))
