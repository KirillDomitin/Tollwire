"""Sync (сверка сервера с БД и починка) и health (только чтение) поверх фейкового сервера."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import SSHConnectionError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.awg_config import append_peer, generate_secret_keys, list_peers, remove_peer
from infrastructure.vpn.config import CONTAINER_NAME, INTERNET_PROBE_COMMAND
from modules.access.models import AccessGrantStatus as S
from modules.access.models.vpn_credential import VPNCredential
from modules.protocols.repository import ProtocolRepository
from modules.servers.health import ServerHealthService
from modules.servers.models.server import HealthStatus, ServerStatus
from modules.servers.sync import ServerSyncService
from tests.fakes.fake_ssh import CONFIG_FILE, AppPeer, FakeServer, FakeSSHClient
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
    # Чужой контейнер с похожим именем, который панель не ищет ни под каким протоколом. (`amnezia-awg2` с 23.2 —
    # это приложение AmneziaVPN, его читает провайдер amnezia_app_awg2; см. тесты ниже.)
    fake.containers[f"{CONTAINER_NAME}-old"] = "running"
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


# --- приложение AmneziaVPN на сервере (этап 23.2): sync и health перебирают и его типы протоколов ---


async def test_sync_of_ordinary_server_reports_only_the_tracked_protocol(world: World) -> None:
    """Без приложения Amnezia sync — как раньше: одна запись, про amnezia_wg; новые типы ничего не добавляют."""
    server, _, fake = await world.server()
    report = await ServerSyncService(world.session).sync(server.id)
    assert [p["type"] for p in report["protocols"]] == ["amnezia_wg"]
    assert report["issues"] == [] and report["protocols"][0]["issues"] == []
    assert not any(c.startswith("docker exec") and "amnezia-awg2" in c for c in fake.commands)


async def test_sync_of_server_without_any_protocol_has_no_app_entries(world: World) -> None:
    server, _, fake = await world.server(install=False)
    report = await ServerSyncService(world.session).sync(server.id)
    assert not any(p["type"].startswith("amnezia_app") for p in report["protocols"])
    assert not any(c.startswith("docker exec") for c in fake.commands)  # контейнеров приложения нет — читать нечего


async def test_sync_reports_installed_amnezia_app_as_untracked(world: World) -> None:
    server, _, fake = await world.server(install=False)
    k = generate_secret_keys().public_key
    fake.add_amnezia_app("amnezia-awg2", peers=[k], clients_table={k: "Phone"}, port=41234)
    report = await ServerSyncService(world.session).sync(server.id)
    entry = {p["type"]: p for p in report["protocols"]}["amnezia_app_awg2"]
    assert entry["in_db"] is False and entry["issues"] == ["Installed on server but not tracked in DB"]
    assert (entry["state"], entry["port"], entry["clients_on_server"]) == ("running", 41234, 1)
    assert "Phone" not in str(report) and k not in str(report)  # в отчёте sync — только счётчики


async def test_sync_reports_v1_app_container_under_its_own_type(world: World) -> None:
    server, _, fake = await world.server(install=False)
    fake.add_amnezia_app("amnezia-awg", peers=[generate_secret_keys().public_key])
    report = await ServerSyncService(world.session).sync(server.id)
    types = {p["type"]: p for p in report["protocols"]}
    assert types["amnezia_app_awg"]["issues"] == ["Installed on server but not tracked in DB"]
    assert "amnezia_app_awg2" not in types or types["amnezia_app_awg2"]["state"] == "not_installed"


async def test_sync_of_amnezia_app_creates_nothing_and_changes_nothing(world: World, session: AsyncSession) -> None:
    server, _, fake = await world.server(install=False)
    app = fake.add_amnezia_app(
        "amnezia-awg2", peers=[generate_secret_keys().public_key, generate_secret_keys().public_key]
    )
    files_before = dict(app.files)
    report = await ServerSyncService(world.session).sync(server.id)
    assert "amnezia_app_awg2" in {p["type"] for p in report["protocols"]}  # приложение сверено (только прочитано)
    assert await ProtocolRepository(session).list_by_server_id(server.id) == []  # в БД протокол не появился
    assert (await session.execute(select(func.count()).select_from(VPNCredential))).scalar_one() == 0
    assert fake.changing_commands() == [] and fake.writes == []
    assert app.files == files_before and fake.containers == {"amnezia-awg2": "running"}


async def test_sync_does_not_reconcile_or_remove_clients_of_amnezia_app(world: World) -> None:
    """Клиенты приложения — «чужие»: sync не добавляет и не убирает их, даже если имя похоже на grant-*."""
    server, _, fake = await world.server(install=False)
    k = generate_secret_keys().public_key
    app = fake.add_amnezia_app("amnezia-awg2", peers=[k], clients_table={k: "grant-deadbeef0000"})
    report = await ServerSyncService(world.session).sync(server.id)
    assert "amnezia_app_awg2" in {p["type"] for p in report["protocols"]}  # приложение сверено (только прочитано)
    assert [peer.public_key for peer in app.peers] == [k] and k in app.files[app.config_path]
    assert fake.changing_commands() == []


async def test_sync_with_stopped_amnezia_app_reports_stopped_and_untracked(world: World) -> None:
    server, _, fake = await world.server(install=False)
    fake.add_amnezia_app("amnezia-awg2", peers=[generate_secret_keys().public_key], running=False)
    report = await ServerSyncService(world.session).sync(server.id)
    entry = {p["type"]: p for p in report["protocols"]}["amnezia_app_awg2"]
    assert entry["state"] == "stopped" and entry["clients_on_server"] == 0
    assert "Installed on server but not tracked in DB" in entry["issues"]


async def test_sync_with_unreadable_amnezia_app_config_still_succeeds(world: World) -> None:
    server, _, fake = await world.server(install=False)
    app = fake.add_amnezia_app("amnezia-awg2", peers=[generate_secret_keys().public_key])
    del app.files[app.config_path]
    report = await ServerSyncService(world.session).sync(server.id)  # не падает
    entry = {p["type"]: p for p in report["protocols"]}["amnezia_app_awg2"]
    assert entry["in_db"] is False and entry["clients_on_server"] == 0


async def test_sync_repairs_panel_clients_and_leaves_amnezia_app_alone(world: World) -> None:
    server, protocol, fake = await world.server()
    _, _, _, credential = await world.issued(protocol)
    fake.files[CONFIG_FILE], _ = remove_peer(fake.files[CONFIG_FILE], credential.client_public_key)
    app = fake.add_amnezia_app("amnezia-awg2", peers=[generate_secret_keys().public_key])
    app_files = dict(app.files)

    report = await ServerSyncService(world.session).sync(server.id)
    types = {p["type"]: p for p in report["protocols"]}
    assert keys(types["amnezia_wg"]["restored"]) == {credential.client_public_key}  # своё чинится, как раньше
    assert types["amnezia_app_awg2"]["issues"] == ["Installed on server but not tracked in DB"]
    assert app.files == app_files
    assert not [c for c in fake.changing_commands() if "amnezia-awg2" in c]  # в контейнер приложения — только чтение


async def test_health_of_ordinary_server_lists_only_the_tracked_protocol(world: World) -> None:
    server, _, _ = await world.server()
    report = await ServerHealthService(world.session).check(server.id)
    assert report["status"] == "ok", report
    assert [p["type"] for p in report["protocols"]] == ["amnezia_wg"]


async def test_health_of_server_without_protocols_has_no_app_entries(world: World) -> None:
    server, _, fake = await world.server(install=False)
    report = await ServerHealthService(world.session).check(server.id)
    assert report["status"] == "ok" and report["protocols"] == []
    assert not any(c.startswith("docker exec") for c in fake.commands)


async def test_health_reports_installed_amnezia_app_as_untracked_without_changing_it(world: World) -> None:
    server, _, fake = await world.server(install=False)
    k = generate_secret_keys().public_key
    app = fake.add_amnezia_app(
        "amnezia-awg2", peers=[AppPeer(k, handshake=int(datetime.now(timezone.utc).timestamp()))]
    )
    report = await ServerHealthService(world.session).check(server.id)
    entry = {p["type"]: p for p in report["protocols"]}["amnezia_app_awg2"]
    assert entry["in_db"] is False and entry["port"] == app.port
    assert entry["clients"] == {"total": 1, "active": 1}
    checks = {c["name"]: c["ok"] for c in entry["checks"]}
    assert checks["container"] is True and checks["config"] is True and checks["interface"] is True
    assert checks["tracked_in_db"] is True  # приложение Amnezia — не поломка: сервер не должен вечно быть DEGRADED
    assert all(checks.values()), checks
    assert [c for c in fake.changing_commands() if c != INTERNET_PROBE_COMMAND] == []  # замер интернета — не изменение
    assert fake.writes == []


async def test_health_with_stopped_amnezia_app_reports_container_down(world: World) -> None:
    server, _, fake = await world.server(install=False)
    fake.add_amnezia_app("amnezia-awg2", peers=[generate_secret_keys().public_key], running=False)
    report = await ServerHealthService(world.session).check(server.id)
    entry = {p["type"]: p for p in report["protocols"]}["amnezia_app_awg2"]
    assert {c["name"]: c["ok"] for c in entry["checks"]}["container"] is False
    assert entry["clients"] == {"total": 0, "active": 0}


async def test_health_report_has_no_amnezia_app_secrets(world: World) -> None:
    server, _, fake = await world.server(install=False)
    app = fake.add_amnezia_app("amnezia-awg2", peers=[generate_secret_keys().public_key])
    report = await ServerHealthService(world.session).check(server.id)
    text = str(report)
    assert "amnezia_app_awg2" in {p["type"] for p in report["protocols"]}  # приложение в отчёте есть
    assert not any(secret in text for secret in app.secrets)
