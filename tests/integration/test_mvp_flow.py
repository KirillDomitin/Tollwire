"""Definition of Done (CLAUDE.md) целиком через HTTP: настоящий вход, API, очередь Redis и воркер; SSH — фейковый.

войти → добавить сервер → проверить SSH → установить Amnezia → установить AmneziaWG → смотреть job/статус →
создать VPN-пользователя → выдать доступ → получить конфиг → изменить срок → отозвать доступ →
удалить пользователя → sync → health → смотреть audit log
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import asyncssh
import httpx

from infrastructure.workers.worker import JobWorker
from tests.integration.conftest import AdminLogin, drain
from tests.unit.test_amnezia_link import decode as decode_share_link


async def run_job(api: httpx.AsyncClient, worker: JobWorker, response: httpx.Response) -> dict[str, Any]:
    """202 → выполнить очередь → вернуть итог задачи через GET /jobs/{id}."""
    assert response.status_code == 202, response.text
    await drain(worker)
    job: dict[str, Any] = (await api.get(f"/api/v1/jobs/{response.json()['id']}")).json()
    return job


async def test_definition_of_done(api: httpx.AsyncClient, worker: JobWorker, admin: AdminLogin) -> None:
    now = datetime.now(timezone.utc)

    # сервер + SSH
    server = (await api.post("/api/v1/servers", json={"name": "de-1", "host": "203.0.113.10"})).json()
    sid = server["id"]
    assert server["status"] == "new"
    assert (await api.put(f"/api/v1/servers/{sid}/credentials", json={"secret": "root-password"})).status_code == 200
    connection = (await api.post(f"/api/v1/servers/{sid}/test-connection")).json()
    assert connection["success"] is True

    # панель ставит на сервер свой SSH-ключ вместо пароля (ключ хоста закреплён: пароль уходит только на проверенный)
    host_key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public().export_public_key().decode()
    assert (await api.put(f"/api/v1/servers/{sid}/host-key", json={"public_key": host_key})).status_code == 200
    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/ssh-key"))
    assert job["status"] == "success", job
    assert job["result"]["replaced"] == "password" and job["result"]["fingerprint"].startswith("SHA256:")
    stored = (await api.get(f"/api/v1/servers/{sid}/credentials")).json()
    assert stored["auth_type"] == "ssh_key" and stored["managed_key"] is True
    assert stored["key_fingerprint"] == job["result"]["fingerprint"]
    assert "secret" not in stored and "PRIVATE KEY" not in str(stored) and "root-password" not in str(job)

    # установка платформы и протокола (через задачи и воркер)
    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/install"))
    assert job["status"] == "success" and job["admin_id"] == str(admin.admin.id)
    assert (await api.get(f"/api/v1/servers/{sid}")).json()["status"] == "ready"
    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/protocol"))
    assert job["status"] == "success", job
    (protocol,) = (await api.get(f"/api/v1/servers/{sid}/protocols")).json()
    assert protocol["type"] == "amnezia_wg" and protocol["port"]

    # пользователь, подписка, доступ, конфиг
    user = (await api.post("/api/v1/vpn-users", json={"username": "alice", "email": "alice@example.com"})).json()
    period = {
        "valid_from": (now - timedelta(minutes=1)).isoformat(),
        "valid_until": (now + timedelta(days=30)).isoformat(),
    }
    subscription = (await api.post(f"/api/v1/vpn-users/{user['id']}/subscriptions", json=period)).json()
    grant = (
        await api.post(f"/api/v1/subscriptions/{subscription['id']}/grants", json={"protocol_id": protocol["id"]})
    ).json()
    assert grant["server_id"] == sid
    job = await run_job(api, worker, await api.post(f"/api/v1/access/{grant['id']}/credentials"))
    assert job["status"] == "success", job
    (credential,) = (await api.get(f"/api/v1/access/{grant['id']}/credentials")).json()
    assert "config" not in credential  # в обычном ответе конфига нет
    reveal = await api.post(f"/api/v1/credentials/{credential['id']}/config")
    assert reveal.status_code == 200 and reveal.headers["cache-control"] == "no-store"
    config = reveal.json()["config"]
    assert f"Endpoint = 203.0.113.10:{protocol['port']}" in config and "PrivateKey" in config
    # Тот же конфиг для приложения AmneziaVPN — с именем сервера из панели вместо «СЕРВЕР 1».
    app_document = decode_share_link(reveal.json()["share_link"])
    assert app_document["description"] == "de-1" and app_document["hostName"] == "203.0.113.10"
    assert len(reveal.json()["share_qr"]) >= 2  # серия QR для сканера приложения

    # смена срока
    new_until = (now + timedelta(days=60)).isoformat()
    patched = (await api.patch(f"/api/v1/subscriptions/{subscription['id']}", json={"valid_until": new_until})).json()
    assert patched["valid_until"].startswith(new_until[:19])

    # отзыв доступа → клиент убран с сервера → пользователя можно удалить
    revoked = (await api.post(f"/api/v1/access/{grant['id']}/revoke")).json()
    assert revoked["status"] == "revoked"
    assert (await api.delete(f"/api/v1/vpn-users/{user['id']}")).status_code == 409  # клиент ещё на сервере
    await drain(worker)
    (credential,) = (await api.get(f"/api/v1/access/{grant['id']}/credentials")).json()
    assert credential["revoked_at"] is not None
    assert (await api.post(f"/api/v1/credentials/{credential['id']}/config")).status_code == 409
    assert (await api.delete(f"/api/v1/vpn-users/{user['id']}")).status_code == 204

    # sync и health
    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/sync"))
    assert sync["status"] == "success" and sync["result"]["protocols"][0]["clients_on_server"] == 0
    health = await run_job(api, worker, await api.post(f"/api/v1/servers/{sid}/health"))
    assert health["status"] == "success" and health["result"]["status"] == "ok", health["result"]
    assert (await api.get(f"/api/v1/servers/{sid}")).json()["health_status"] == "ok"

    # журнал аудита
    logs = (await api.get("/api/v1/audit-logs", params={"admin_id": str(admin.admin.id), "limit": 100})).json()
    actions = {log["action"] for log in logs}
    assert {
        "login",
        "server_created",
        "server_credentials_set",
        "server_ssh_key_install_started",
        "server_ssh_key_install_completed",
        "server_install_started",
        "server_install_completed",
        "protocol_install_completed",
        "vpn_user_created",
        "subscription_created",
        "subscription_updated",
        "access_granted",
        "credential_issued",
        "config_viewed",
        "access_revoked",
        "credential_revoked",
        "vpn_user_deleted",
        "server_sync_completed",
    } <= actions
    dump = str(logs)
    # секреты в журнал не попали (ни пароль, ни приватный ключ панели, ни ключ сервера в конфигах)
    assert "root-password" not in dump and "PrivateKey" not in dump and "PRIVATE KEY" not in dump


async def test_api_error_mapping(api: httpx.AsyncClient, worker: JobWorker) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await api.get(f"/api/v1/servers/{missing}")).status_code == 404
    assert (await api.get(f"/api/v1/servers/{missing}/protocols")).status_code == 404
    assert (await api.get(f"/api/v1/jobs/{missing}")).status_code == 404
    # статус гранта напрямую не меняется: только /revoke, планировщик и выдача клиента
    assert (await api.patch(f"/api/v1/access/{missing}", json={"status": "active"})).status_code == 405
    assert (await api.put(f"/api/v1/servers/{missing}/credentials", json={"secret": "x"})).status_code == 404

    server = (await api.post("/api/v1/servers", json={"name": "no-creds", "host": "203.0.113.11"})).json()
    assert (await api.post("/api/v1/servers", json={"name": "no-creds", "host": "h"})).status_code == 409
    for action in ("install", "protocol", "sync", "health"):
        assert (await api.post(f"/api/v1/servers/{server['id']}/{action}")).status_code == 409  # нет SSH-учётки

    user = (await api.post("/api/v1/vpn-users", json={"username": "bob", "email": "bob@example.com"})).json()
    assert (
        await api.post("/api/v1/vpn-users", json={"username": "bob2", "email": "bob@example.com"})
    ).status_code == 409
    assert (
        await api.post(f"/api/v1/vpn-users/{user['id']}/subscriptions", json={"valid_from": "nope"})
    ).status_code == 422
    assert (await api.post("/api/v1/vpn-users", json={"username": "x", "email": "not-an-email"})).status_code == 422
    assert (await api.get("/api/v1/audit-logs", params={"limit": 1000})).status_code == 422


async def test_issue_before_period_is_409(api: httpx.AsyncClient, worker: JobWorker) -> None:
    server = (await api.post("/api/v1/servers", json={"name": "de-2", "host": "203.0.113.12"})).json()
    await api.put(f"/api/v1/servers/{server['id']}/credentials", json={"secret": "pw"})
    await run_job(api, worker, await api.post(f"/api/v1/servers/{server['id']}/protocol"))
    (protocol,) = (await api.get(f"/api/v1/servers/{server['id']}/protocols")).json()
    user = (await api.post("/api/v1/vpn-users", json={"username": "carol", "email": "carol@example.com"})).json()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    period = {"valid_from": start.isoformat(), "valid_until": (start + timedelta(days=30)).isoformat()}
    subscription = (await api.post(f"/api/v1/vpn-users/{user['id']}/subscriptions", json=period)).json()
    grant = (
        await api.post(f"/api/v1/subscriptions/{subscription['id']}/grants", json={"protocol_id": protocol["id"]})
    ).json()
    response = await api.post(f"/api/v1/access/{grant['id']}/credentials")
    assert response.status_code == 409 and "not started" in response.json()["detail"]


async def test_user_list_shows_latest_subscription(api: httpx.AsyncClient) -> None:
    """Список пользователей сразу показывает доступ: последнюю подписку (статус, срок) или null без подписок."""
    now = datetime.now(timezone.utc)
    dave = (await api.post("/api/v1/vpn-users", json={"username": "dave", "email": "dave@example.com"})).json()
    erin = (await api.post("/api/v1/vpn-users", json={"username": "erin", "email": "erin@example.com"})).json()
    period = {"valid_from": now.isoformat(), "valid_until": (now + timedelta(days=30)).isoformat()}
    old = (await api.post(f"/api/v1/vpn-users/{dave['id']}/subscriptions", json=period)).json()
    assert (await api.post(f"/api/v1/subscriptions/{old['id']}/revoke")).status_code == 200
    longer = {"valid_from": now.isoformat(), "valid_until": (now + timedelta(days=90)).isoformat()}
    new = (await api.post(f"/api/v1/vpn-users/{dave['id']}/subscriptions", json=longer)).json()

    response = await api.get("/api/v1/vpn-users")
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()}
    assert by_id[erin["id"]]["access"] is None
    access = by_id[dave["id"]]["access"]
    assert access["id"] == new["id"]  # отозванная старая подписка не заслоняет новую
    assert access["status"] == new["status"]
    assert datetime.fromisoformat(access["valid_until"]) == datetime.fromisoformat(new["valid_until"])
