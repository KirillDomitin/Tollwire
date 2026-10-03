"""Sync (сверка сервера с БД и починка) и health (только чтение) поверх фейкового сервера."""

from datetime import datetime, timedelta, timezone

import pytest

from core.exceptions import SSHConnectionError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.awg_config import append_peer, generate_secret_keys, list_peers, remove_peer
from infrastructure.vpn.config import CONTAINER_NAME
from modules.access.models import AccessGrantStatus as S
from modules.servers.health import ServerHealthService
from modules.servers.models.server import HealthStatus, ServerStatus
from modules.servers.sync import ServerSyncService
from tests.fakes.fake_ssh import CONFIG_FILE, FakeServer, FakeSSHClient
from tests.fixtures import World


def keys(items: list[dict[str, object]]) -> set[object]:
    return {item["public_key"] for item in items}


async def test_sync_repairs_server_by_database(world: World) -> None:
    server, protocol, fake = await world.server()
    _, _, grant_a, cred_a = await world.issued(protocol)  # пропадёт с сервера → восстановить
    _, _, grant_b, cred_b = await world.issued(protocol)  # отозван только в БД → убрать с сервера
    _, sub_c, grant_c, cred_c = await world.issued(protocol)  # грант отозван, отзыв не дошёл → довести
    _, sub_d, grant_d, cred_d = await world.issued(protocol)  # срок по датам кончился, статус ещё ACTIVE
    _, _, _, cred_ok = await world.issued(protocol)  # всё в порядке

    fake.files[CONFIG_FILE], _ = remove_peer(fake.files[CONFIG_FILE], cred_a.client_public_key)
    cred_b.revoked_at = datetime.now(timezone.utc)
    grant_c.status = S.REVOKED
    sub_d.valid_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    protocol.port = 1  # порт в БД разошёлся с сервером
    server.status = ServerStatus.OFFLINE
    await world.session.commit()
    async with FakeSSHClient(fake) as ssh:
        orphan = await AmneziaWGProvider(ssh).create_client("grant-deadbeef0000")
    foreign = generate_secret_keys().public_key
    fake.files[CONFIG_FILE] = append_peer(
        fake.files[CONFIG_FILE], f"[Peer]\nPublicKey = {foreign}\nAllowedIPs = 10.8.1.200/32\n"
    )

    report = await ServerSyncService(world.session).sync(server.id)
    (result,) = report["protocols"]
    assert keys(result["restored"]) == {cred_a.client_public_key}
    assert keys(result["removed_revoked"]) == {cred_b.client_public_key}
    assert keys(result["revocations_completed"]) == {cred_c.client_public_key, cred_d.client_public_key}
    assert keys(result["removed_orphans"]) == {orphan.public_key}
    assert keys(result["foreign"]) == {foreign}
    assert keys(result["ok"]) == {cred_ok.client_public_key}
    assert result["port_updated_from"] == 1 and report["server_status"] == "ready"

    on_server = {peer.public_key for peer in list_peers(fake.files[CONFIG_FILE])}
    assert on_server == {cred_a.client_public_key, cred_ok.client_public_key, foreign}
    assert fake.interface_peers == on_server  # изменения применены к живому интерфейсу
    for credential in (cred_c, cred_d):
        await world.session.refresh(credential)
        assert credential.revoked_at is not None
    await world.session.refresh(protocol)
    assert protocol.port == result["port"]

    again = (await ServerSyncService(world.session).sync(server.id))["protocols"][0]
    changes = ("restored", "removed_revoked", "removed_orphans", "revocations_completed")
    assert not any(again[key] for key in changes) and keys(again["foreign"]) == {foreign}  # повтор ничего не меняет


async def test_sync_restored_peer_keeps_working_config(world: World) -> None:
    server, protocol, fake = await world.server()
    _, _, grant, credential = await world.issued(protocol)
    fake.files[CONFIG_FILE], _ = remove_peer(fake.files[CONFIG_FILE], credential.client_public_key)
    await ServerSyncService(world.session).sync(server.id)
    (peer,) = list_peers(fake.files[CONFIG_FILE])
    assert (peer.public_key, peer.address) == (credential.client_public_key, credential.client_address)


async def test_sync_does_not_touch_clients_when_container_stopped(world: World) -> None:
    server, protocol, fake = await world.server()
    await world.issued(protocol)
    fake.containers[CONTAINER_NAME] = "exited"
    before = fake.files[CONFIG_FILE]
    (result,) = (await ServerSyncService(world.session).sync(server.id))["protocols"]
    assert result["issues"] == ["Container is not running, clients were not reconciled"]
    assert fake.files[CONFIG_FILE] == before


async def test_sync_reports_untracked_installation(world: World) -> None:
    server, _, fake = await world.server(install=False)
    async with FakeSSHClient(fake) as ssh:
        await AmneziaWGProvider(ssh).install()  # поставили мимо панели — строки протокола в БД нет
    (result,) = (await ServerSyncService(world.session).sync(server.id))["protocols"]
    assert result["in_db"] is False and result["issues"] == ["Installed on server but not tracked in DB"]


async def test_sync_unreachable_marks_offline(world: World) -> None:
    server, _, fake = await world.server()
    fake.reachable = False
    with pytest.raises(SSHConnectionError):
        await ServerSyncService(world.session).sync(server.id)
    await world.session.refresh(server)
    assert server.status == ServerStatus.OFFLINE


async def test_health_ok_then_degraded(world: World) -> None:
    server, protocol, fake = await world.server()
    report = await ServerHealthService(world.session).check(server.id)
    assert report["status"] == "ok", report
    assert report["internet_latency_ms"] == 42
    await world.session.refresh(server)
    assert server.health_status == HealthStatus.OK and server.last_health_at is not None

    fake.nat_rule = False
    protocol.port = 1
    await world.session.commit()
    report = await ServerHealthService(world.session).check(server.id)
    failed = {c["name"] for p in report["protocols"] for c in p["checks"] if not c["ok"]}
    assert report["status"] == "degraded" and failed == {"nat", "port_matches_db"}
    await world.session.refresh(protocol)
    assert protocol.port == 1  # health только смотрит — ничего не чинит


async def test_health_down_is_a_result_not_an_error(world: World) -> None:
    server, _, fake = await world.server()
    fake.reachable = False
    report = await ServerHealthService(world.session).check(server.id)  # не бросает исключение
    assert report["status"] == "down" and report["checks"][0]["name"] == "ssh"
    await world.session.refresh(server)
    assert (server.status, server.health_status) == (ServerStatus.OFFLINE, HealthStatus.DOWN)


async def test_health_without_protocol_reports_platform_only(world: World) -> None:
    server, _, fake = await world.server(install=False)
    fake.containers[f"{CONTAINER_NAME}2"] = "running"  # чужой контейнер с похожим именем
    report = await ServerHealthService(world.session).check(server.id)
    assert report["status"] == "ok" and report["protocols"] == []


async def test_reachable_again_returns_ready(world: World) -> None:
    server, _, fake = await world.server()
    fake.reachable = False
    await ServerHealthService(world.session).check(server.id)
    fake.reachable = True
    report = await ServerHealthService(world.session).check(server.id)
    assert report["server_status"] == "ready"


def test_fake_server_rejects_unknown_commands() -> None:
    """Страховка самого фейка: неожиданная команда — громкая ошибка, а не тихий пустой ответ."""
    with pytest.raises(AssertionError):
        FakeServer().run("curl http://evil")
