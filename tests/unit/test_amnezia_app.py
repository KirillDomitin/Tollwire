"""Провайдеры чужих установок AmneziaVPN (этап 23.2): контейнеры `amnezia-awg` (v1) и `amnezia-awg2`. Настоящий код
провайдера поверх FakeServer; они ТОЛЬКО ЧИТАЮТ — ни одной изменяющей команды, секреты сервера не выходят наружу.

Новые имена импортируются внутри хелперов: пока кода нет, красными становятся тесты, а не вся коллекция."""

import dataclasses
import importlib
import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from core import ssh_exception
from core.ssh_exception import CommandExecutionError, SSHServerError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.awg_config import generate_secret_keys, render_client_config
from infrastructure.vpn.provider import ProviderState, VPNProvider
from tests.fakes.fake_ssh import (
    APP_CLIENTS_TABLE,
    APP_PRIVATE_KEY_FILE,
    APP_PSK_FILE,
    AmneziaApp,
    AppPeer,
    FakeServer,
    FakeSSHClient,
    clients_table_json,
)

CLASS_NAMES = {"amnezia-awg": "AmneziaAppAWGProvider", "amnezia-awg2": "AmneziaAppAWG2Provider"}
OLD_HANDSHAKE = 1_700_000_000


def provider_for(container: str, ssh: FakeSSHClient) -> VPNProvider:
    module = importlib.import_module("infrastructure.vpn.amnezia_app")
    return getattr(module, CLASS_NAMES[container])(ssh)  # type: ignore[no-any-return]


async def call(fake: FakeServer, container: str, method: str, *args: object) -> Any:
    async with FakeSSHClient(fake) as ssh:
        return await getattr(provider_for(container, ssh), method)(*args)


def provider_module() -> Any:
    return importlib.import_module("infrastructure.vpn.provider")


@pytest.fixture(params=sorted(CLASS_NAMES))
def container(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@dataclass
class Scenario:
    fake: FakeServer
    app: AmneziaApp
    container: str
    named: str  # peer с именем в clientsTable, handshake свежий
    nameless: str  # peer, которого в clientsTable нет, handshake давний
    wide: str  # peer с двумя адресами в AllowedIPs — панель такого не поддерживает
    never: str  # peer, у которого рукопожатий не было
    now: int  # момент создания сценария: «свежее» рукопожатие считается от него, а не от старта прогона


@pytest.fixture
def scenario(container: str) -> Scenario:
    named, nameless, wide, never = (generate_secret_keys().public_key for _ in range(4))
    now = int(datetime.now(timezone.utc).timestamp())
    fake = FakeServer()
    app = fake.add_amnezia_app(
        container,
        peers=[
            AppPeer(named, "10.8.1.2/32", handshake=now),
            AppPeer(nameless, "10.8.1.3/32", handshake=OLD_HANDSHAKE),
            AppPeer(wide, "10.8.1.4/32, 10.8.1.5/32", handshake=now),
            AppPeer(never, "10.8.1.6/32"),
        ],
        clients_table={named: "Phone", wide: "Wide", never: "Never"},
        port=41234,
    )
    return Scenario(fake, app, container, named, nameless, wide, never, now)


def discovered_by_key(discovery: Any) -> dict[str, Any]:
    return {client.public_key: client for client in discovery.clients}


# --- состав кода ---


def test_unsupported_operation_error_is_an_ssh_server_error() -> None:
    error_class = getattr(ssh_exception, "UnsupportedOperationError", None)
    assert error_class is not None, "core.ssh_exception.UnsupportedOperationError is not defined"
    assert issubclass(error_class, SSHServerError)


def test_both_providers_share_a_base_class_and_are_vpn_providers() -> None:
    module = importlib.import_module("infrastructure.vpn.amnezia_app")
    v1, v2 = (getattr(module, name) for name in CLASS_NAMES.values())
    assert issubclass(v1, VPNProvider) and issubclass(v2, VPNProvider) and v1 is not v2
    shared = (set(v1.__mro__) & set(v2.__mro__)) - {object, VPNProvider} - set(VPNProvider.__mro__)
    assert shared, "providers must have a common base class of their own"


def test_discovered_client_is_a_frozen_dataclass_without_secrets() -> None:
    cls = provider_module().DiscoveredClient
    assert dataclasses.is_dataclass(cls)
    assert {f.name for f in dataclasses.fields(cls)} == {
        "public_key",
        "address",
        "name",
        "latest_handshake",
        "supported",
    }
    client = cls(public_key="k", address="10.8.1.2", name=None, latest_handshake=None, supported=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        client.name = "changed"


def test_discovery_has_the_agreed_fields() -> None:
    cls = provider_module().Discovery
    assert dataclasses.is_dataclass(cls)
    assert {f.name for f in dataclasses.fields(cls)} == {"container", "state", "image", "port", "clients"}


def test_discover_is_optional_for_providers_and_defaults_to_none() -> None:
    assert "discover" not in VPNProvider.__abstractmethods__  # type: ignore[attr-defined]
    assert hasattr(VPNProvider, "discover")


async def test_own_provider_is_not_subject_to_discovery_and_runs_no_commands() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg2")
    async with FakeSSHClient(fake) as ssh:
        assert await AmneziaWGProvider(ssh).discover() is None  # type: ignore[attr-defined]
    assert fake.commands == []


# --- контейнера нет ---


async def test_no_container_is_not_installed_with_no_clients(container: str) -> None:
    fake = FakeServer()
    status = await call(fake, container, "get_status")
    assert status.server_status == ProviderState.NOT_INSTALLED
    assert (status.version, status.port, status.uptime) == (None, None, None)
    assert await call(fake, container, "list_clients") == []
    snapshot = await call(fake, container, "sync")
    assert snapshot.provider_status.server_status == ProviderState.NOT_INSTALLED and snapshot.client_list == []
    health = await call(fake, container, "health_check")
    assert health.installed is False and (health.clients_total, health.clients_active) == (0, 0)


async def test_no_container_discover_returns_none(container: str) -> None:
    assert await call(FakeServer(), container, "discover") is None


async def test_no_container_runs_no_docker_exec(container: str) -> None:
    fake = FakeServer()
    for method in ("get_status", "list_clients", "sync", "health_check", "discover"):
        await call(fake, container, method)
    assert fake.changing_commands() == [] and not any(c.startswith("docker exec") for c in fake.commands)


async def test_container_with_similar_name_is_not_taken(container: str) -> None:
    """Имя ищется точно: у v1 нет права на amnezia-awg2 и наоборот; близкие имена не подходят никому."""
    other = "amnezia-awg2" if container == "amnezia-awg" else "amnezia-awg"
    fake = FakeServer()
    fake.add_amnezia_app(other, peers=[generate_secret_keys().public_key])
    fake.containers[f"{container}-old"] = "running"
    fake.containers[f"x{container}"] = "running"
    assert (await call(fake, container, "get_status")).server_status == ProviderState.NOT_INSTALLED
    assert await call(fake, container, "discover") is None
    assert (await call(fake, container, "health_check")).installed is False


async def test_panel_own_container_is_not_an_amnezia_app() -> None:
    """Контейнер amnezia-awg, поставленный самой панелью (папка панели среди монтирований), — не приложение: иначе
    sync и health каждого обычного сервера сообщали бы «установлено мимо панели»."""
    fake = FakeServer()
    async with FakeSSHClient(fake) as ssh:
        await AmneziaWGProvider(ssh).install()
    fake.commands.clear()
    writes_before = list(fake.writes)
    assert (await call(fake, "amnezia-awg", "get_status")).server_status == ProviderState.NOT_INSTALLED
    assert await call(fake, "amnezia-awg", "discover") is None
    assert await call(fake, "amnezia-awg", "list_clients") == []
    assert (await call(fake, "amnezia-awg", "health_check")).installed is False
    assert fake.writes == writes_before and fake.changing_commands() == []
    assert not any(c.startswith("docker exec") for c in fake.commands)  # в свой контейнер за чужим конфигом не ходим


# --- работающее приложение ---


async def test_status_of_running_app(scenario: Scenario) -> None:
    status = await call(scenario.fake, scenario.container, "get_status")
    assert status.server_status == ProviderState.RUNNING
    assert status.version == scenario.app.image  # версия — образ контейнера
    assert status.port == 41234  # ListenPort из конфига внутри контейнера
    assert status.uptime  # «Up 5 minutes» из docker ps


async def test_list_clients_takes_names_from_clients_table_and_handshakes_from_the_interface(
    scenario: Scenario,
) -> None:
    clients = {c.public_key: c for c in await call(scenario.fake, scenario.container, "list_clients")}
    assert set(clients) == {scenario.named, scenario.nameless, scenario.wide, scenario.never}
    assert clients[scenario.named].name == "Phone" and clients[scenario.named].address == "10.8.1.2"
    assert clients[scenario.nameless].name is None  # в таблице нет — имя неизвестно, а не придумано
    assert clients[scenario.named].latest_handshake == datetime.fromtimestamp(scenario.now, tz=timezone.utc)
    assert clients[scenario.nameless].latest_handshake == datetime.fromtimestamp(OLD_HANDSHAKE, tz=timezone.utc)
    assert clients[scenario.never].latest_handshake is None  # 0 — рукопожатий не было


async def test_clients_table_in_legacy_object_format_is_read(container: str) -> None:
    fake = FakeServer()
    key = generate_secret_keys().public_key
    fake.add_amnezia_app(container, peers=[key], clients_table=clients_table_json({key: "Old phone"}, legacy=True))
    (client,) = await call(fake, container, "list_clients")
    assert client.name == "Old phone"


@pytest.mark.parametrize(
    "table", ["not json", "{", "[1, 2, 3]", "null", "x" * 5000], ids=["text", "cut", "ints", "null", "big"]
)
async def test_broken_clients_table_leaves_clients_unnamed_but_listed(container: str, table: str) -> None:
    fake = FakeServer()
    key = generate_secret_keys().public_key
    fake.add_amnezia_app(container, peers=[key], clients_table=table)
    (client,) = await call(fake, container, "list_clients")
    assert (client.public_key, client.name) == (key, None)


async def test_missing_clients_table_leaves_clients_unnamed_but_listed(container: str) -> None:
    fake = FakeServer()
    key = generate_secret_keys().public_key
    app = fake.add_amnezia_app(container, peers=[key])
    del app.files[APP_CLIENTS_TABLE]
    (client,) = await call(fake, container, "list_clients")
    assert (client.public_key, client.name) == (key, None)


async def test_failed_handshake_command_is_not_fatal(scenario: Scenario) -> None:
    scenario.app.interface_up = False  # `show` падает: рукопожатия просто неизвестны
    clients = await call(scenario.fake, scenario.container, "list_clients")
    assert len(clients) == 4 and all(c.latest_handshake is None for c in clients)
    assert {c.name for c in clients} >= {"Phone"}  # имена не пострадали


async def test_discover_reports_container_state_image_port_and_clients(scenario: Scenario) -> None:
    discovery = await call(scenario.fake, scenario.container, "discover")
    assert discovery.container == scenario.container
    assert discovery.state == ProviderState.RUNNING
    assert discovery.image == scenario.app.image and discovery.port == 41234
    by_key = discovered_by_key(discovery)
    assert list(by_key) == [scenario.named, scenario.nameless, scenario.wide, scenario.never]  # порядок конфига
    named = by_key[scenario.named]
    assert (named.address, named.name, named.supported) == ("10.8.1.2", "Phone", True)
    assert named.latest_handshake == datetime.fromtimestamp(scenario.now, tz=timezone.utc)
    assert by_key[scenario.nameless].name is None and by_key[scenario.nameless].supported is True
    assert by_key[scenario.never].latest_handshake is None


async def test_discover_marks_peer_with_several_allowed_ips_unsupported_but_lists_it(scenario: Scenario) -> None:
    by_key = discovered_by_key(await call(scenario.fake, scenario.container, "discover"))
    assert by_key[scenario.wide].supported is False and by_key[scenario.wide].name == "Wide"
    assert [k for k, c in by_key.items() if not c.supported] == [scenario.wide]


async def test_discover_clients_are_the_agreed_dataclass(scenario: Scenario) -> None:
    discovery = await call(scenario.fake, scenario.container, "discover")
    cls = provider_module().DiscoveredClient
    assert all(isinstance(client, cls) for client in discovery.clients) and discovery.clients


async def test_discover_does_not_cut_off_peers_when_config_has_none(container: str) -> None:
    fake = FakeServer()
    fake.add_amnezia_app(container)
    discovery = await call(fake, container, "discover")
    assert discovery.state == ProviderState.RUNNING and discovery.clients == []


async def test_both_app_containers_are_read_independently() -> None:
    v1_key, v2_key = generate_secret_keys().public_key, generate_secret_keys().public_key
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg", peers=[v1_key], clients_table={v1_key: "V1 phone"})
    fake.add_amnezia_app("amnezia-awg2", peers=[v2_key], clients_table={v2_key: "V2 phone"}, port=2222)
    first = await call(fake, "amnezia-awg", "discover")
    second = await call(fake, "amnezia-awg2", "discover")
    assert [(c.public_key, c.name) for c in first.clients] == [(v1_key, "V1 phone")]
    assert [(c.public_key, c.name) for c in second.clients] == [(v2_key, "V2 phone")]
    assert (first.container, second.container, second.port) == ("amnezia-awg", "amnezia-awg2", 2222)


# --- остановленный контейнер / сломанные части ---


async def test_stopped_container_is_stopped_and_clients_cannot_be_read(scenario: Scenario) -> None:
    scenario.fake.containers[scenario.container] = "exited"
    status = await call(scenario.fake, scenario.container, "get_status")
    assert status.server_status == ProviderState.STOPPED
    assert await call(scenario.fake, scenario.container, "list_clients") == []
    snapshot = await call(scenario.fake, scenario.container, "sync")
    assert snapshot.provider_status.server_status == ProviderState.STOPPED and snapshot.client_list == []


async def test_stopped_container_discover_returns_discovery_with_no_clients(scenario: Scenario) -> None:
    scenario.fake.containers[scenario.container] = "exited"
    discovery = await call(scenario.fake, scenario.container, "discover")
    assert discovery is not None
    assert (discovery.container, discovery.state, discovery.image) == (
        scenario.container,
        ProviderState.STOPPED,
        scenario.app.image,
    )
    assert discovery.clients == []


async def test_stopped_container_health_is_installed_but_container_check_fails(scenario: Scenario) -> None:
    scenario.fake.containers[scenario.container] = "exited"
    health = await call(scenario.fake, scenario.container, "health_check")
    checks = {c.name: c.ok for c in health.checks}
    assert health.installed is True and checks["container"] is False
    assert (health.clients_total, health.clients_active) == (0, 0)


async def test_unreadable_config_in_running_container_gives_empty_discovery(scenario: Scenario) -> None:
    del scenario.app.files[scenario.app.config_path]
    discovery = await call(scenario.fake, scenario.container, "discover")
    assert discovery is not None and discovery.state == ProviderState.RUNNING and discovery.clients == []
    assert await call(scenario.fake, scenario.container, "list_clients") == []


async def test_health_all_checks_pass_for_healthy_app(scenario: Scenario) -> None:
    health = await call(scenario.fake, scenario.container, "health_check")
    checks = {c.name: c.ok for c in health.checks}
    assert {"container", "config", "interface"} <= set(checks)
    assert all(checks.values()), checks
    assert health.installed is True and health.port == 41234
    assert health.clients_total == 4 and health.clients_active == 2  # named и wide — рукопожатие в пределах окна


async def test_health_reports_interface_down(scenario: Scenario) -> None:
    scenario.app.interface_up = False
    health = await call(scenario.fake, scenario.container, "health_check")
    checks = {c.name: c.ok for c in health.checks}
    assert checks["interface"] is False and checks["container"] is True and checks["config"] is True


async def test_health_reports_unreadable_config(scenario: Scenario) -> None:
    del scenario.app.files[scenario.app.config_path]
    health = await call(scenario.fake, scenario.container, "health_check")
    checks = {c.name: c.ok for c in health.checks}
    assert checks["config"] is False and health.installed is True


async def test_sync_snapshot_matches_status_and_clients(scenario: Scenario) -> None:
    snapshot = await call(scenario.fake, scenario.container, "sync")
    assert snapshot.provider_status.server_status == ProviderState.RUNNING and snapshot.provider_status.port == 41234
    assert {c.public_key for c in snapshot.client_list} == {
        scenario.named,
        scenario.nameless,
        scenario.wide,
        scenario.never,
    }


@pytest.mark.parametrize("method", ["get_status", "discover"])
async def test_failed_container_listing_is_an_error_not_an_empty_server(scenario: Scenario, method: str) -> None:
    """Как в 23.1: упавший `docker ps` — ошибка, а не «контейнеров нет» (иначе поиск соврал бы «Amnezia нет»)."""
    scenario.fake.ps_fails = True
    with pytest.raises(CommandExecutionError):
        await call(scenario.fake, scenario.container, method)


# --- только чтение ---


async def test_install_uninstall_and_client_changes_are_unsupported_and_touch_nothing(scenario: Scenario) -> None:
    error_class = getattr(ssh_exception, "UnsupportedOperationError", None)
    assert error_class is not None, "core.ssh_exception.UnsupportedOperationError is not defined"
    keys = generate_secret_keys()
    client_config = render_client_config(
        server_config=scenario.app.render_config(),
        client_private_key=keys.private_key,
        client_address=ipaddress.IPv4Address("10.8.1.9"),
        psk=keys.psk,
        server_public_key=generate_secret_keys().public_key,
        endpoint_host="203.0.113.10",
    )
    scenario.fake.commands.clear()
    for method, args in [
        ("install", ()),
        ("uninstall", ()),
        ("create_client", ("grant-aaa",)),
        ("revoke_client", (scenario.named,)),
        ("restore_client", ("grant-aaa", client_config)),
    ]:
        with pytest.raises(error_class):
            await call(scenario.fake, scenario.container, method, *args)
    assert scenario.fake.commands == []  # ни одной команды на сервере — даже проверки контейнера
    assert scenario.fake.writes == []
    assert scenario.fake.containers[scenario.container] == "running"


async def test_unsupported_error_text_has_no_secrets(scenario: Scenario) -> None:
    error_class = getattr(ssh_exception, "UnsupportedOperationError", None)
    assert error_class is not None
    for method, args in [("install", ()), ("uninstall", ()), ("create_client", ("grant-aaa",))]:
        with pytest.raises(error_class) as raised:
            await call(scenario.fake, scenario.container, method, *args)
        assert not any(secret in str(raised.value) for secret in scenario.app.secrets)


async def test_reading_runs_no_changing_command_and_writes_no_file(scenario: Scenario) -> None:
    for method in ("get_status", "list_clients", "sync", "health_check", "discover"):
        await call(scenario.fake, scenario.container, method)
    assert scenario.fake.changing_commands() == []  # docker exec cat / show latest-handshakes — чтение
    assert scenario.fake.writes == [] and scenario.fake.dirs == {}
    assert scenario.fake.containers[scenario.container] == "running"


async def test_stopped_container_is_never_started(scenario: Scenario) -> None:
    scenario.fake.containers[scenario.container] = "exited"
    for method in ("get_status", "list_clients", "sync", "health_check", "discover"):
        await call(scenario.fake, scenario.container, method)
    assert scenario.fake.containers[scenario.container] == "exited"
    assert not any(c.startswith(("docker start", "docker restart", "docker run")) for c in scenario.fake.commands)


async def test_only_config_and_clients_table_are_read_inside_the_container(scenario: Scenario) -> None:
    for method in ("get_status", "list_clients", "sync", "health_check", "discover"):
        await call(scenario.fake, scenario.container, method)
    paths = {path for _, path in scenario.fake.container_reads}
    assert paths <= {scenario.app.config_path, APP_CLIENTS_TABLE}, paths
    assert {container for container, _ in scenario.fake.container_reads} == {scenario.container}


async def test_files_with_server_private_key_and_psk_are_never_read(scenario: Scenario) -> None:
    for method in ("get_status", "list_clients", "sync", "health_check", "discover"):
        await call(scenario.fake, scenario.container, method)
    forbidden = (APP_PRIVATE_KEY_FILE, APP_PSK_FILE)
    assert not any(path in forbidden for _, path in scenario.fake.container_reads)
    assert not any("private_key" in c or "psk" in c.lower() for c in scenario.fake.commands)


async def test_no_executed_command_contains_a_client_name(container: str) -> None:
    fake = FakeServer()
    keys = [generate_secret_keys().public_key for _ in range(3)]
    hostile = {keys[0]: "$(reboot)", keys[1]: "x; rm -rf /", keys[2]: "Phone"}
    fake.add_amnezia_app(container, peers=keys, clients_table=hostile)
    for method in ("get_status", "list_clients", "sync", "health_check", "discover"):
        await call(fake, container, method)
    assert not any(name in command for command in fake.commands for name in hostile.values())
    assert not any(key in command for command in fake.commands for key in keys)  # и публичных ключей клиентов тоже
    assert fake.changing_commands() == []


async def test_all_commands_are_built_from_constants(scenario: Scenario) -> None:
    """Команды: docker ps/inspect и docker exec <контейнер> cat <путь> | <утилита> show <интерфейс> ..."""
    for method in ("get_status", "list_clients", "sync", "health_check", "discover"):
        await call(scenario.fake, scenario.container, method)
    execs = [" ".join(c.split()) for c in scenario.fake.commands if c.startswith("docker exec")]
    assert execs, "the provider should read the container"
    tool, interface = scenario.app.tool, scenario.app.interface
    allowed = {
        f"docker exec {scenario.container} cat {scenario.app.config_path}",
        f"docker exec {scenario.container} cat {APP_CLIENTS_TABLE}",
        f"docker exec {scenario.container} {tool} show {interface} latest-handshakes",
    }
    assert set(execs) <= allowed, set(execs) - allowed


# --- секреты не утекают ---


async def test_server_private_key_and_psk_are_in_no_result(scenario: Scenario) -> None:
    results = [
        await call(scenario.fake, scenario.container, method)
        for method in ("get_status", "list_clients", "sync", "health_check", "discover")
    ]
    text = "\n".join(repr(result) for result in results) + "\n".join(str(result) for result in results)
    for secret in scenario.app.secrets:
        assert secret not in text
    assert "PrivateKey" not in text and "PresharedKey" not in text  # и самих строк конфига нет


async def test_discovery_clients_hold_nothing_but_the_agreed_fields(scenario: Scenario) -> None:
    discovery = await call(scenario.fake, scenario.container, "discover")
    for client in discovery.clients:
        assert {f.name for f in dataclasses.fields(client)} == {
            "public_key",
            "address",
            "name",
            "latest_handshake",
            "supported",
        }


async def test_secrets_are_not_logged(scenario: Scenario, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for method in ("get_status", "list_clients", "sync", "health_check", "discover"):
        await call(scenario.fake, scenario.container, method)
    assert not any(secret in caplog.text for secret in scenario.app.secrets)
    assert "PrivateKey" not in caplog.text and "PresharedKey" not in caplog.text


async def test_errors_do_not_quote_the_config(scenario: Scenario) -> None:
    """Любая ошибка чтения (здесь — упавший docker ps) не должна цитировать содержимое конфига."""
    scenario.fake.ps_fails = True
    for method in ("get_status", "discover"):
        try:
            await call(scenario.fake, scenario.container, method)
        except SSHServerError as exc:
            assert not any(secret in str(exc) for secret in scenario.app.secrets)
