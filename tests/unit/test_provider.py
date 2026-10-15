"""AmneziaWGProvider поверх фейкового сервера: настоящий код провайдера, вместо сети — FakeServer."""

from datetime import datetime, timezone

import pytest

from core.ssh_exception import CommandExecutionError
from infrastructure.ssh.client import CommandResult
from infrastructure.vpn.amnezia import AmneziaInstaller, AmneziaWGProvider
from infrastructure.vpn.awg_config import interface_values, list_peers, parse_client_config, remove_peer
from infrastructure.vpn.config import CONTAINER_NAME, NAT_POST_UP
from infrastructure.vpn.provider import ProviderState
from tests.fakes.fake_ssh import CONFIG_FILE, FakeServer, FakeSSHClient


@pytest.fixture
async def server() -> FakeServer:
    fake = FakeServer()
    async with FakeSSHClient(fake) as ssh:
        await AmneziaWGProvider(ssh).install()
    return fake


async def call(fake: FakeServer, method: str, *args: object) -> object:
    async with FakeSSHClient(fake) as ssh:
        return await getattr(AmneziaWGProvider(ssh), method)(*args)


async def test_install_writes_config_starts_container_and_nat(server: FakeServer) -> None:
    assert server.running and server.nat_rule
    status = await call(server, "get_status")
    assert status.server_status == ProviderState.RUNNING  # type: ignore[attr-defined]
    assert status.port == int(interface_values(server.files[CONFIG_FILE])["ListenPort"])  # type: ignore[attr-defined]


async def test_reinstall_keeps_identity(server: FakeServer) -> None:
    before = server.files[CONFIG_FILE]
    await call(server, "install")
    assert server.files[CONFIG_FILE] == before  # ключи и порт сервера не перегенерированы


async def test_reinstall_patches_nat_into_old_config(server: FakeServer) -> None:
    server.files[CONFIG_FILE] = "\n".join(line for line in server.files[CONFIG_FILE].splitlines() if "Post" not in line)
    server.nat_rule = False
    await call(server, "install")
    assert NAT_POST_UP in server.files[CONFIG_FILE] and server.nat_rule


async def test_new_container_restarts_after_server_reboot(server: FakeServer) -> None:
    assert server.restart_policy == "unless-stopped"  # без политики VPN не поднимется после перезагрузки сервера


async def test_reinstall_sets_restart_policy_on_old_container(server: FakeServer) -> None:
    server.restart_policy = "no"  # контейнер, созданный старой версией панели
    await call(server, "install")
    assert server.restart_policy == "unless-stopped"
    assert server.running and any("docker update" in c for c in server.commands)
    assert not any(c.startswith(("docker stop", "docker restart")) for c in server.commands)  # клиентов не рвём


async def test_reinstall_leaves_other_restart_policy_alone(server: FakeServer) -> None:
    server.restart_policy = "always"  # выбрано вручную — это тоже перезапуск, не трогаем
    await call(server, "install")
    assert server.restart_policy == "always"


async def test_health_flags_missing_restart_policy(server: FakeServer) -> None:
    server.restart_policy = "no"
    checks = {c.name: c.ok for c in (await call(server, "health_check")).checks}  # type: ignore[attr-defined]
    assert checks["restart_policy"] is False


async def test_foreign_container_with_similar_name_is_not_ours() -> None:
    fake = FakeServer(containers={f"{CONTAINER_NAME}2": "running"})  # как amnezia-awg2 на my-real
    status = await call(fake, "get_status")
    assert status.server_status == ProviderState.NOT_INSTALLED  # type: ignore[attr-defined]
    health = await call(fake, "health_check")
    assert health.installed is False  # type: ignore[attr-defined]


async def test_create_client_adds_applied_peer_and_valid_config(server: FakeServer) -> None:
    first = await call(server, "create_client", "grant-aaa")
    second = await call(server, "create_client", "grant-bbb")
    assert (first.address, second.address) == ("10.8.1.1", "10.8.1.2")  # type: ignore[attr-defined]
    assert server.interface_peers == {first.public_key, second.public_key}  # type: ignore[attr-defined]
    params = parse_client_config(first.config)  # type: ignore[attr-defined]
    assert params.public_key == first.public_key  # type: ignore[attr-defined]
    port = interface_values(server.files[CONFIG_FILE])["ListenPort"]
    assert f"Endpoint = {server.host}:{port}" in first.config  # type: ignore[attr-defined]
    assert {peer.name for peer in list_peers(server.files[CONFIG_FILE])} == {"grant-aaa", "grant-bbb"}


@pytest.mark.parametrize("name", ["bad name", "x; rm -rf /", "$(id)", "a" * 33, "", "grant-aaa\n"])
async def test_create_client_rejects_unsafe_names(server: FakeServer, name: str) -> None:
    with pytest.raises(ValueError):
        await call(server, "create_client", name)


async def test_revoke_client_is_idempotent(server: FakeServer) -> None:
    client = await call(server, "create_client", "grant-aaa")
    await call(server, "revoke_client", client.public_key)  # type: ignore[attr-defined]
    assert server.interface_peers == set() and list_peers(server.files[CONFIG_FILE]) == []
    writes_before = len(server.commands)
    await call(server, "revoke_client", client.public_key)  # type: ignore[attr-defined]
    assert not any("syncconf" in cmd for cmd in server.commands[writes_before:])  # нечего применять


async def test_revoke_client_rejects_garbage_key(server: FakeServer) -> None:
    with pytest.raises(ValueError):
        await call(server, "revoke_client", "x; reboot")


async def test_restore_client_brings_back_same_key(server: FakeServer) -> None:
    client = await call(server, "create_client", "grant-aaa")
    server.files[CONFIG_FILE], _ = remove_peer(server.files[CONFIG_FILE], client.public_key)  # type: ignore[attr-defined]
    assert await call(server, "restore_client", "grant-aaa", client.config) is True  # type: ignore[attr-defined]
    (peer,) = list_peers(server.files[CONFIG_FILE])
    assert (peer.public_key, peer.address) == (client.public_key, client.address)  # type: ignore[attr-defined]
    assert client.public_key in server.interface_peers  # type: ignore[attr-defined]
    assert await call(server, "restore_client", "grant-aaa", client.config) is False  # type: ignore[attr-defined]


async def test_restore_client_refuses_taken_address(server: FakeServer) -> None:
    client = await call(server, "create_client", "grant-aaa")
    server.files[CONFIG_FILE], _ = remove_peer(server.files[CONFIG_FILE], client.public_key)  # type: ignore[attr-defined]
    await call(server, "create_client", "grant-bbb")  # занял тот же адрес 10.8.1.1
    with pytest.raises(CommandExecutionError):
        await call(server, "restore_client", "grant-aaa", client.config)  # type: ignore[attr-defined]


async def test_list_clients_and_sync_snapshot(server: FakeServer) -> None:
    client = await call(server, "create_client", "grant-aaa")
    server.handshakes[client.public_key] = 1_700_000_000  # type: ignore[attr-defined]
    (info,) = await call(server, "list_clients")  # type: ignore[misc]
    assert info.name == "grant-aaa" and info.address == "10.8.1.1"
    assert info.latest_handshake == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    snapshot = await call(server, "sync")
    assert snapshot.provider_status.server_status == ProviderState.RUNNING  # type: ignore[attr-defined]
    assert [c.public_key for c in snapshot.client_list] == [client.public_key]  # type: ignore[attr-defined]


async def test_health_all_ok(server: FakeServer) -> None:
    client = await call(server, "create_client", "grant-aaa")
    server.handshakes[client.public_key] = int(datetime.now(timezone.utc).timestamp())  # type: ignore[attr-defined]
    health = await call(server, "health_check")
    assert {check.name for check in health.checks} == {  # type: ignore[attr-defined]
        "container",
        "config",
        "interface",
        "listen_port",
        "port_published",
        "nat",
        "restart_policy",
    }
    assert all(check.ok for check in health.checks)  # type: ignore[attr-defined]
    assert (health.clients_total, health.clients_active) == (1, 1)  # type: ignore[attr-defined]


async def test_health_reports_broken_parts(server: FakeServer) -> None:
    server.nat_rule = False
    server.port_published = False
    failed = {check.name for check in (await call(server, "health_check")).checks if not check.ok}  # type: ignore[attr-defined]
    assert failed == {"nat", "port_published"}


async def test_health_stopped_container(server: FakeServer) -> None:
    server.containers[CONTAINER_NAME] = "exited"
    health = await call(server, "health_check")
    names = {check.name: check.ok for check in health.checks}  # type: ignore[attr-defined]
    # дальше без живого контейнера не проверить; политика перезапуска читается и у остановленного
    assert names == {"container": False, "config": True, "restart_policy": True}
    assert health.installed  # type: ignore[attr-defined]


async def test_uninstall_removes_everything(server: FakeServer) -> None:
    status = await call(server, "uninstall")
    assert status.server_status == ProviderState.NOT_INSTALLED  # type: ignore[attr-defined]
    assert CONTAINER_NAME not in server.containers and CONFIG_FILE not in server.files


async def test_installer_platform_checks() -> None:
    fake = FakeServer(docker_installed=False, docker_active=False, internet=False)
    async with FakeSSHClient(fake) as ssh:
        installer = AmneziaInstaller(ssh)
        assert await installer.docker_version() is None
        assert await installer.internet_latency_ms() is None
        await installer.ensure_ready()  # ставит и запускает Docker
        assert await installer.docker_version() and await installer.docker_daemon_active()
    fake.internet = True
    async with FakeSSHClient(fake) as ssh:
        assert await AmneziaInstaller(ssh).internet_latency_ms() == 42


async def test_docker_install_gets_long_timeout() -> None:
    """apt-get на свежем сервере идёт минуты: с общим таймаутом команды (30 с) установка падала, хотя apt на
    сервере продолжал работу. Таймаут этой команды — свой, длинный."""
    timeouts: dict[str, int | None] = {}

    class Recording(FakeSSHClient):
        async def execute(self, command: str, timeout: int | None = None) -> CommandResult:
            timeouts[command] = timeout
            return await super().execute(command, timeout)

    async with Recording(FakeServer(docker_installed=False)) as ssh:
        await AmneziaInstaller(ssh).ensure_ready()
    (apt_timeout,) = [t for command, t in timeouts.items() if command.startswith("apt-get")]
    assert apt_timeout is not None and apt_timeout >= 600
