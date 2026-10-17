"""Запись в контейнер приложения AmneziaVPN (этап 23.3) — ЕДИНСТВЕННОЕ место, где панель пишет на чужой сервер, поэтому
проверки строгие: что именно записано (байт в байт), какие команды выполнены (только константы), что осталось на хосте
(ничего), что с секретами (нигде), и что будет при сбое на каждом шаге.

Протокол: прочитать конфиг -> проверить -> перечитать (гонка с приложением) -> SFTP-файл в /opt/vpn-admin/stage ->
`docker cp` -> `rm -f` (всегда) -> `<утилита> syncconf` (сбой -> откат) -> то же для clientsTable (только после
применения; не JSON-массив или нет файла — таблицу не трогаем).

Новые имена (revoke_client приложения, restore_peer, ConfigChangedError, render_app_peer_block) берутся внутри хелперов:
пока кода нет, красными становятся тесты, а не вся коллекция."""

import importlib
import json
import logging
import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from core import ssh_exception
from core.exceptions import SSHConnectionError
from core.ssh_exception import CommandExecutionError, SSHServerError, UnsupportedOperationError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.awg_config import discover_peers, generate_secret_keys, list_peers, render_peer_block
from infrastructure.vpn.provider import VPNProvider
from tests.fakes.fake_ssh import (
    APP_CLIENTS_TABLE,
    STAGE_DIR,
    AmneziaApp,
    AppPeer,
    FakeServer,
    FakeSSHClient,
    apply_command,
    clients_table_json,
    stage_conf,
    stage_table,
)

CLASS_NAMES = {"amnezia-awg": "AmneziaAppAWGProvider", "amnezia-awg2": "AmneziaAppAWG2Provider"}
NEW_ADDRESS = "10.8.1.9"
NEW_NAME = "Newphone"


# --- хелперы -------------------------------------------------------------------------------------------------------


def provider_for(container: str, ssh: FakeSSHClient) -> VPNProvider:
    module = importlib.import_module("infrastructure.vpn.amnezia_app")
    return getattr(module, CLASS_NAMES[container])(ssh)  # type: ignore[no-any-return]


async def call(fake: FakeServer, container: str, method: str, *args: object) -> Any:
    async with FakeSSHClient(fake) as ssh:
        return await getattr(provider_for(container, ssh), method)(*args)


def config_changed_error() -> type[Exception]:
    error_class = getattr(ssh_exception, "ConfigChangedError", None)
    assert error_class is not None, "core.ssh_exception.ConfigChangedError is not defined"
    return error_class  # type: ignore[no-any-return]


def error_text(exc: BaseException) -> str:
    """Весь текст исключения и его причин: str, repr, args — по ним ищем утечки секретов."""
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(parts) < 30:
        parts += [str(current), repr(current), repr(current.args)]
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


def peer_block(key: str, psk: str, ips: str, extra: str = "") -> str:
    return f"[Peer]\nPublicKey = {key}\nPresharedKey = {psk}\nAllowedIPs = {ips}\n{extra}"


def build_config(interface: str, blocks: list[str]) -> str:
    return interface + "".join("\n" + block for block in blocks)


@pytest.fixture(params=sorted(CLASS_NAMES))
def container(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@dataclass
class Rig:
    """Приложение с четырьмя peer'ами и сложным конфигом: обфускация, комментарий, PostUp, чужие поля в [Peer]."""

    fake: FakeServer
    app: AmneziaApp
    container: str
    a: str  # первый peer (в таблице: Alice)
    b: str  # средний, с PersistentKeepalive (Bob)
    c: str  # peer, которого в таблице нет
    d: str  # последний, два адреса в AllowedIPs (Wide)
    ghost: str  # запись таблицы без peer'а в конфиге
    interface: str
    blocks: dict[str, str]
    original: str
    original_table: str
    secrets: list[str]  # серверный приватный ключ и PSK всех peer'ов на момент создания

    def config(self, *without: str) -> str:
        keys = (self.a, self.b, self.c, self.d)
        return build_config(self.interface, [self.blocks[k] for k in keys if k not in without])

    def table(self) -> Any:
        return json.loads(self.app.files[APP_CLIENTS_TABLE])

    def live_keys(self) -> list[str]:
        return [peer.public_key for peer in self.app.peers]


@pytest.fixture
def rig(container: str) -> Rig:
    a, b, c, d, ghost = (generate_secret_keys().public_key for _ in range(5))
    fake = FakeServer()
    app = fake.add_amnezia_app(
        container,
        peers=[
            AppPeer(a, "10.8.1.2/32", handshake=1_700_000_001),
            AppPeer(b, "10.8.1.3/32", handshake=1_700_000_002),
            AppPeer(c, "10.8.1.4/32"),
            AppPeer(d, "10.8.1.5/32, 10.8.1.6/32"),
        ],
        port=41234,
    )
    interface = (
        f"[Interface]\nPrivateKey = {app.private_key}\nAddress = 10.8.1.0/24\nListenPort = {app.port}\n"
        "# keep this comment\n"
        "Jc = 5\nJmin = 10\nJmax = 50\nS1 = 60\nS2 = 77\nS3 = 11\nS4 = 22\n"
        "H1 = 1020325451\nH2 = 3288052141\nH3 = 1766607858\nH4 = 2528465083\n"
        "I1 = <b 0xf6ab3267fa><r 16>\nPostUp = iptables -A FORWARD -i %i -j ACCEPT\n"
    )
    blocks = {
        p.public_key: peer_block(
            p.public_key, p.psk, p.allowed_ips, "PersistentKeepalive = 25\n" if p.public_key == b else ""
        )
        for p in app.peers
    }
    original = build_config(interface, list(blocks.values()))
    table = [
        {"clientId": a, "userData": {"clientName": "Alice", "creationDate": "Mon Jan 1 12:00:00 2024"}},
        {"clientId": ghost, "userData": {"clientName": "Ghost"}},
        {"clientId": b, "userData": {"clientName": "Bob"}},
        {"clientId": d, "userData": {"clientName": "Wide", "allowedIps": "10.8.1.5"}},
    ]
    app.files[app.config_path] = original
    app.files[APP_CLIENTS_TABLE] = json.dumps(table, indent=4)
    return Rig(
        fake, app, container, a, b, c, d, ghost, interface, blocks, original, app.files[APP_CLIENTS_TABLE],
        list(app.secrets),
    )  # fmt: skip


@dataclass
class NewClient:
    key: str
    psk: str


@pytest.fixture
def new() -> NewClient:
    keys = generate_secret_keys()
    return NewClient(keys.public_key, keys.psk)


def new_block(client: NewClient, address: str = NEW_ADDRESS) -> str:
    return f"[Peer]\nPublicKey = {client.key}\nPresharedKey = {client.psk}\nAllowedIPs = {address}/32\n"


async def revoke(rig: Rig, key: str) -> Any:
    return await call(rig.fake, rig.container, "revoke_client", key)


async def restore(rig: Rig, client: NewClient, address: str = NEW_ADDRESS, name: str = NEW_NAME) -> Any:
    return await call(rig.fake, rig.container, "restore_peer", name, client.key, client.psk, address)


def allowed_commands(app: AmneziaApp) -> set[str]:
    c = app.container
    return {
        f"docker exec {c} cat {app.config_path}",
        f"docker exec {c} cat {APP_CLIENTS_TABLE}",
        f"docker exec {c} {app.tool} show {app.interface} latest-handshakes",
        f"docker cp {stage_conf(c)} {c}:{app.config_path}",
        f"docker cp {stage_table(c)} {c}:{APP_CLIENTS_TABLE}",
        f"rm -f {stage_conf(c)}",
        f"rm -f {stage_table(c)}",
        f"rm -f {STAGE_DIR}/{c}.*",
        f"mkdir -p {posixpath.dirname(STAGE_DIR)}",
        apply_command(c, app.tool, app.interface, app.config_path),
    }


def syncconf_command(app: AmneziaApp) -> str:
    return apply_command(app.container, app.tool, app.interface, app.config_path)


def normalized(fake: FakeServer) -> list[str]:
    return [" ".join(command.split()) for command in fake.commands]


def assert_clean(rig: Rig, extra_secrets: tuple[str, ...] = ()) -> None:
    """Инварианты ЛЮБОЙ операции записи — успешной или нет."""
    fake, app = rig.fake, rig.app
    assert fake.stage_files() == [], "stage files with the server private key are left on the host"
    assert fake.unexpected_changing_commands() == []
    for command in normalized(fake):
        if command.startswith("docker ps -a --filter"):
            assert f"'name=^/?{app.container}$'" in command
        elif command.startswith("docker inspect"):
            assert command.endswith(f" {app.container}"), command
        else:
            assert command in allowed_commands(app), f"command is not a known constant: {command!r}"
    assert set(fake.writes) <= {stage_conf(app.container), stage_table(app.container)}  # SFTP — только в stage
    assert all(copy.mode == 0o600 for copy in fake.copies)
    assert all(copy.container == app.container for copy in fake.copies)
    for secret in (*rig.secrets, *extra_secrets):
        assert not any(secret in command for command in fake.commands), "secret in a command"
        assert not any(secret in text for text in fake.files.values()), "secret left in a host file"


def assert_container_untouched(rig: Rig, realigned: bool = False) -> None:
    """Ни конфиг, ни таблица, ни живое состояние не менялись и ничего не копировалось в контейнер.

    realigned: идемпотентная операция («менять нечего») всё равно один раз применяет файл к интерфейсу, чтобы
    вылечить расхождение после оборванной прошлой операции; содержимое при этом не меняется."""
    assert rig.app.files[rig.app.config_path] == rig.original
    assert rig.app.files[APP_CLIENTS_TABLE] == rig.original_table
    assert rig.live_keys() == [rig.a, rig.b, rig.c, rig.d]
    assert rig.app.applies <= (1 if realigned else 0) and rig.fake.copies == []


def assert_untouched(rig: Rig, realigned: bool = False) -> None:
    """То же, и на хосте ничего не писали (даже промежуточных файлов)."""
    assert_container_untouched(rig, realigned)
    assert rig.fake.writes == []


def only_reads(rig: Rig) -> bool:
    """Из изменяющих команд допустимо только выравнивающее применение конфига (см. assert_container_untouched)."""
    return set(rig.fake.changing_commands()) <= {syncconf_command(rig.app)}


def table_commands(rig: Rig) -> list[str]:
    return [c for c in normalized(rig.fake) if "clientsTable" in c and not c.startswith("docker exec")]


# --- состав кода ---------------------------------------------------------------------------------------------------


def test_config_changed_error_is_a_command_execution_error() -> None:
    assert issubclass(config_changed_error(), CommandExecutionError)


def test_restore_peer_is_a_non_abstract_method_of_the_base_provider() -> None:
    assert hasattr(VPNProvider, "restore_peer")
    assert "restore_peer" not in VPNProvider.__abstractmethods__  # type: ignore[attr-defined]


async def test_base_and_panel_providers_do_not_support_restore_peer_and_run_nothing() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg2")
    async with FakeSSHClient(fake) as ssh:
        with pytest.raises(UnsupportedOperationError):
            await AmneziaWGProvider(ssh).restore_peer(  # type: ignore[attr-defined]
                NEW_NAME, generate_secret_keys().public_key, generate_secret_keys().psk, NEW_ADDRESS
            )
    assert fake.commands == [] and fake.writes == []


async def test_unsupported_operations_stay_unsupported_and_touch_nothing(rig: Rig) -> None:
    for method, args in [
        ("install", ()),
        ("uninstall", ()),
        ("create_client", ("grant-aaa",)),
        ("restore_client", ("grant-aaa", rig.original)),
    ]:
        with pytest.raises(UnsupportedOperationError):
            await call(rig.fake, rig.container, method, *args)
    assert rig.fake.commands == [] and rig.fake.writes == []  # ни одной команды — даже проверки контейнера
    assert_untouched(rig)


# --- awg_config.render_app_peer_block ------------------------------------------------------------------------------


def render_app_peer_block() -> Callable[..., str]:
    module = importlib.import_module("infrastructure.vpn.awg_config")
    function = getattr(module, "render_app_peer_block", None)
    assert function is not None, "awg_config.render_app_peer_block is not defined"
    return function  # type: ignore[no-any-return]


def test_app_peer_block_has_the_exact_format_of_the_app_and_no_name_comment() -> None:
    keys = generate_secret_keys()
    block = render_app_peer_block()(keys.public_key, keys.psk, NEW_ADDRESS)
    expected = f"[Peer]\nPublicKey = {keys.public_key}\nPresharedKey = {keys.psk}\nAllowedIPs = {NEW_ADDRESS}/32\n"
    assert block.lstrip("\n") == expected  # пустая строка перед блоком допустима, остальное — до символа
    assert block.endswith("\n") and not block.endswith("\n\n")
    assert "#" not in block and "client" not in block


def test_app_peer_block_differs_from_the_panel_block_only_by_the_name_comment() -> None:
    import ipaddress

    keys = generate_secret_keys()
    app_block = render_app_peer_block()(keys.public_key, keys.psk, NEW_ADDRESS).lstrip("\n")
    panel_block = render_peer_block("someone", keys.public_key, keys.psk, ipaddress.IPv4Address(NEW_ADDRESS))
    assert panel_block.replace("# client: someone\n", "") == app_block


def test_app_peer_block_is_read_back_as_a_nameless_supported_peer() -> None:
    keys = generate_secret_keys()
    config = "[Interface]\nPrivateKey = x\nAddress = 10.8.1.0/24\n\n" + render_app_peer_block()(
        keys.public_key, keys.psk, NEW_ADDRESS
    )
    (peer,) = list_peers(config)
    assert (peer.public_key, peer.address, peer.name) == (keys.public_key, NEW_ADDRESS, None)
    (found,) = discover_peers(config)
    assert found.supported is True and found.address == NEW_ADDRESS


# --- revoke_client: основной сценарий ------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["a", "b", "c", "d"])
async def test_revoke_removes_exactly_that_peer_and_leaves_the_rest_byte_for_byte(rig: Rig, which: str) -> None:
    key = getattr(rig, which)
    assert await revoke(rig, key) is None
    assert rig.app.files[rig.app.config_path] == rig.config(key)  # весь прочий текст — прежний
    assert key not in rig.app.files[rig.app.config_path]
    assert_clean(rig)


async def test_revoke_of_the_only_peer_leaves_the_interface_section_intact() -> None:
    fake = FakeServer()
    key = generate_secret_keys().public_key
    app = fake.add_amnezia_app("amnezia-awg2", peers=[key])
    interface = app.render_config().split("\n[Peer]")[0].rstrip("\n") + "\n"
    await call(fake, "amnezia-awg2", "revoke_client", key)
    assert app.files[app.config_path] == interface
    assert fake.stage_files() == [] and fake.unexpected_changing_commands() == []


async def test_revoke_keeps_obfuscation_postup_comment_and_foreign_peer_fields(rig: Rig) -> None:
    await revoke(rig, rig.a)
    config = rig.app.files[rig.app.config_path]
    for line in ("# keep this comment", "I1 = <b 0xf6ab3267fa><r 16>", "S3 = 11", "S4 = 22", "H4 = 2528465083"):
        assert line in config
    assert "PostUp = iptables -A FORWARD -i %i -j ACCEPT" in config and "PersistentKeepalive = 25" in config
    for key in (rig.b, rig.c, rig.d):
        assert rig.blocks[key] in config  # блок чужого peer'а, вместе с его PresharedKey, цел


async def test_revoke_applies_the_config_to_the_live_interface(rig: Rig) -> None:
    await revoke(rig, rig.b)
    assert rig.live_keys() == [rig.a, rig.c, rig.d] and rig.app.applies == 1
    handshakes = rig.fake.run(f"docker exec {rig.container} {rig.app.tool} show {rig.app.interface} latest-handshakes")
    assert rig.b not in handshakes.stdout and rig.a in handshakes.stdout
    assert [c.public_key for c in await call(rig.fake, rig.container, "list_clients")] == [rig.a, rig.c, rig.d]


async def test_revoke_follows_the_write_protocol_in_order(rig: Rig) -> None:
    await revoke(rig, rig.b)
    commands = normalized(rig.fake)
    cp_conf = f"docker cp {stage_conf(rig.container)} {rig.container}:{rig.app.config_path}"
    cp_table = f"docker cp {stage_table(rig.container)} {rig.container}:{APP_CLIENTS_TABLE}"
    rm_conf, rm_table = f"rm -f {stage_conf(rig.container)}", f"rm -f {stage_table(rig.container)}"
    apply = syncconf_command(rig.app)
    for command in (cp_conf, rm_conf, apply, cp_table, rm_table):
        assert commands.count(command) == 1, command
    assert commands.index(cp_conf) < commands.index(rm_conf)
    assert commands.index(cp_conf) < commands.index(apply) < commands.index(cp_table) < commands.index(rm_table)
    assert commands.index(cp_table) < commands.index(rm_table)
    assert rig.fake.writes == [stage_conf(rig.container), stage_table(rig.container)]  # SFTP, оба — в stage
    (conf_copy, table_copy) = rig.fake.copies
    assert conf_copy.content == rig.config(rig.b) and conf_copy.mode == 0o600 and table_copy.mode == 0o600


async def test_revoke_rereads_the_config_before_writing(rig: Rig) -> None:
    await revoke(rig, rig.b)
    assert rig.app.read_counts[rig.app.config_path] >= 2


async def test_revoke_removes_the_client_from_the_clients_table_keeping_the_others_in_order(rig: Rig) -> None:
    await revoke(rig, rig.b)
    assert [e["clientId"] for e in rig.table()] == [rig.a, rig.ghost, rig.d]
    original = json.loads(rig.original_table)
    assert rig.table() == [original[0], original[1], original[3]]  # записи не пересобраны, поля целы


async def test_revoke_removes_every_duplicate_table_entry_of_the_client(rig: Rig) -> None:
    table = json.loads(rig.original_table)
    table.append({"clientId": rig.b, "userData": {"clientName": "Bob again"}})
    rig.app.files[APP_CLIENTS_TABLE] = json.dumps(table)
    await revoke(rig, rig.b)
    assert rig.b not in [e["clientId"] for e in rig.table()] and len(rig.table()) == 3


async def test_clients_table_is_not_touched_until_the_config_is_applied(rig: Rig) -> None:
    seen: list[str] = []
    rig.app.on_apply = lambda: seen.append(rig.app.files[APP_CLIENTS_TABLE])
    await revoke(rig, rig.b)
    assert seen == [rig.original_table]


# --- revoke_client: идемпотентность и чужое ------------------------------------------------------------------------


async def test_revoke_of_an_unknown_key_writes_nothing_at_all(rig: Rig) -> None:
    assert await revoke(rig, generate_secret_keys().public_key) is None
    assert only_reads(rig) and rig.fake.stage_files() == []
    assert_untouched(rig, realigned=True)
    assert_clean(rig)


async def test_revoke_twice_second_call_writes_nothing(rig: Rig) -> None:
    await revoke(rig, rig.b)
    config, table = rig.app.files[rig.app.config_path], rig.app.files[APP_CLIENTS_TABLE]
    rig.fake.commands.clear()
    rig.fake.copies.clear()
    rig.fake.writes.clear()
    applies = rig.app.applies
    await revoke(rig, rig.b)
    assert only_reads(rig) and rig.fake.copies == [] and rig.fake.writes == [] and rig.app.applies <= applies + 1
    assert rig.app.files[rig.app.config_path] == config and rig.app.files[APP_CLIENTS_TABLE] == table


async def test_revoke_of_a_key_left_only_in_the_clients_table_cleans_the_table_but_not_the_config(rig: Rig) -> None:
    await revoke(rig, rig.ghost)
    assert rig.app.files[rig.app.config_path] == rig.original and rig.app.applies <= 1  # конфиг не трогали
    assert rig.ghost not in [e["clientId"] for e in rig.table()] and len(rig.table()) == 3
    assert not any(c.startswith("docker cp") and c.endswith(rig.app.config_path) for c in normalized(rig.fake))
    assert_clean(rig)


async def test_revoke_does_not_touch_a_key_that_differs_in_case(rig: Rig) -> None:
    swapped = rig.b.swapcase()
    assert swapped != rig.b
    await revoke(rig, swapped)
    assert_untouched(rig, realigned=True)
    assert only_reads(rig)


async def test_revoke_in_one_app_container_never_touches_the_other_one() -> None:
    v1_key, v2_key = generate_secret_keys().public_key, generate_secret_keys().public_key
    fake = FakeServer()
    v1 = fake.add_amnezia_app("amnezia-awg", peers=[v1_key], clients_table={v1_key: "V1"})
    v2 = fake.add_amnezia_app("amnezia-awg2", peers=[v2_key], clients_table={v2_key: "V2"})
    v1_before, v2_before = dict(v1.files), dict(v2.files)
    await call(fake, "amnezia-awg2", "revoke_client", v1_key)  # ключ чужого контейнера: здесь его нет
    assert v1.files == v1_before and v2.files == v2_before and fake.unexpected_changing_commands() == []
    await call(fake, "amnezia-awg2", "revoke_client", v2_key)
    assert v1.files == v1_before and v1.applies == 0 and v2_key not in v2.files[v2.config_path]
    assert not any(c.startswith("docker exec amnezia-awg ") for c in normalized(fake))  # v1 даже не читали


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "A" * 43,  # нет '=' на конце
        "A" * 44,
        "A" * 42 + "=",
        "{key}\n",
        "{key}\n[Peer]\nPublicKey = {key}",
        "{key} ",
        "../../etc/passwd",
        "$(reboot)=",
        "{key}; rm -rf /",
    ],
    ids=["empty", "short", "no-equals", "44", "42", "newline", "injection", "space", "path", "subshell", "semicolon"],
)
async def test_revoke_of_a_malformed_key_is_a_value_error_with_no_commands(rig: Rig, bad: str) -> None:
    with pytest.raises(ValueError):
        await revoke(rig, bad.format(key=rig.b))
    assert rig.fake.commands == []
    assert_untouched(rig)


# --- restore_peer: основной сценарий -------------------------------------------------------------------------------


async def test_restore_peer_appends_exactly_the_app_format_block_and_returns_true(rig: Rig, new: NewClient) -> None:
    assert await restore(rig, new) is True
    # пустая строка, [Peer], PublicKey, PresharedKey, AllowedIPs = <адрес>/32; ни комментария с именем, ни прочего
    assert rig.app.files[rig.app.config_path] == rig.original + "\n" + new_block(new)
    assert "# client" not in rig.app.files[rig.app.config_path] and NEW_NAME not in rig.app.files[rig.app.config_path]
    assert_clean(rig, (new.psk,))


async def test_restore_peer_separates_the_block_with_one_blank_line_when_the_file_has_no_final_newline(
    rig: Rig, new: NewClient
) -> None:
    rig.app.files[rig.app.config_path] = rig.original.rstrip("\n")
    rig.original = rig.app.files[rig.app.config_path]
    await restore(rig, new)
    assert rig.app.files[rig.app.config_path] == rig.original + "\n\n" + new_block(new)


async def test_restore_peer_into_a_config_without_peers() -> None:
    fake = FakeServer()
    app = fake.add_amnezia_app("amnezia-awg2")
    interface = app.render_config()
    keys = generate_secret_keys()
    assert await call(fake, "amnezia-awg2", "restore_peer", NEW_NAME, keys.public_key, keys.psk, NEW_ADDRESS) is True
    assert app.files[app.config_path] == interface + "\n" + new_block(NewClient(keys.public_key, keys.psk))


async def test_restore_peer_makes_the_peer_live_and_visible_with_its_name(rig: Rig, new: NewClient) -> None:
    await restore(rig, new)
    assert rig.live_keys() == [rig.a, rig.b, rig.c, rig.d, new.key] and rig.app.applies == 1
    clients = {c.public_key: c for c in await call(rig.fake, rig.container, "list_clients")}
    assert (clients[new.key].address, clients[new.key].name, clients[new.key].latest_handshake) == (
        NEW_ADDRESS,
        NEW_NAME,
        None,
    )
    assert clients[rig.a].latest_handshake is not None  # рукопожатия прежних peer'ов не потеряны


async def test_restore_peer_adds_the_table_entry_in_the_app_format_after_the_others(rig: Rig, new: NewClient) -> None:
    await restore(rig, new)
    assert rig.table()[:4] == json.loads(rig.original_table)
    assert rig.table()[4:] == [{"clientId": new.key, "userData": {"clientName": NEW_NAME}}]


async def test_restore_peer_follows_the_write_protocol_in_order(rig: Rig, new: NewClient) -> None:
    await restore(rig, new)
    commands = normalized(rig.fake)
    cp_conf = f"docker cp {stage_conf(rig.container)} {rig.container}:{rig.app.config_path}"
    cp_table = f"docker cp {stage_table(rig.container)} {rig.container}:{APP_CLIENTS_TABLE}"
    apply = syncconf_command(rig.app)
    assert commands.index(cp_conf) < commands.index(f"rm -f {stage_conf(rig.container)}")
    assert commands.index(cp_conf) < commands.index(apply) < commands.index(cp_table)
    assert commands.index(cp_table) < commands.index(f"rm -f {stage_table(rig.container)}")
    assert rig.app.read_counts[rig.app.config_path] >= 2


async def test_restore_then_revoke_returns_config_and_table_to_the_original(rig: Rig, new: NewClient) -> None:
    await restore(rig, new)
    await revoke(rig, new.key)
    assert rig.app.files[rig.app.config_path] == rig.original
    assert rig.table() == json.loads(rig.original_table)
    assert rig.live_keys() == [rig.a, rig.b, rig.c, rig.d]
    assert_clean(rig, (new.psk,))


@pytest.mark.parametrize("address", ["10.8.1.1", "10.8.1.7", "10.8.1.254"])
async def test_restore_peer_accepts_any_free_host_address_of_the_subnet(rig: Rig, new: NewClient, address: str) -> None:
    assert await restore(rig, new, address) is True
    assert rig.app.files[rig.app.config_path].endswith(new_block(new, address))


async def test_restore_peer_uses_the_subnet_of_the_interface_not_a_fixed_one(new: NewClient) -> None:
    fake = FakeServer()
    app = fake.add_amnezia_app("amnezia-awg2")
    app.files[app.config_path] = app.files[app.config_path].replace("10.8.1.0/24", "172.29.172.1/24")
    assert await call(fake, "amnezia-awg2", "restore_peer", NEW_NAME, new.key, new.psk, "172.29.172.5") is True
    assert app.files[app.config_path].endswith("AllowedIPs = 172.29.172.5/32\n")
    before = app.files[app.config_path]
    other = generate_secret_keys()
    with pytest.raises(ValueError):  # чужая подсеть
        await call(fake, "amnezia-awg2", "restore_peer", NEW_NAME, other.public_key, other.psk, NEW_ADDRESS)
    assert app.files[app.config_path] == before


# --- restore_peer: идемпотентность и отказы ------------------------------------------------------------------------


async def test_restore_peer_of_an_existing_peer_returns_false_and_writes_nothing(rig: Rig) -> None:
    again = NewClient(rig.b, generate_secret_keys().psk)
    assert await restore(rig, again, "10.8.1.3") is False
    assert only_reads(rig)
    assert_untouched(rig, realigned=True)


async def test_restore_peer_of_an_existing_key_with_another_address_is_still_false_and_writes_nothing(
    rig: Rig,
) -> None:
    assert await restore(rig, NewClient(rig.b, generate_secret_keys().psk), NEW_ADDRESS) is False
    assert only_reads(rig)
    assert_untouched(rig, realigned=True)


async def test_restore_peer_twice_second_call_is_false_and_writes_nothing(rig: Rig, new: NewClient) -> None:
    assert await restore(rig, new) is True
    config, table, applies = rig.app.files[rig.app.config_path], rig.app.files[APP_CLIENTS_TABLE], rig.app.applies
    rig.fake.commands.clear()
    rig.fake.copies.clear()
    rig.fake.writes.clear()
    assert await restore(rig, new) is False
    assert only_reads(rig) and rig.fake.copies == [] and rig.fake.writes == [] and rig.app.applies <= applies + 1
    assert rig.app.files[rig.app.config_path] == config and rig.app.files[APP_CLIENTS_TABLE] == table


@pytest.mark.parametrize("taken", ["10.8.1.2", "10.8.1.3", "10.8.1.4", "10.8.1.5", "10.8.1.6"])
async def test_restore_peer_on_an_address_of_another_peer_is_a_command_error_and_writes_nothing(
    rig: Rig, new: NewClient, taken: str
) -> None:
    """В том числе второй адрес peer'а с двумя адресами в AllowedIPs (10.8.1.6)."""
    with pytest.raises(CommandExecutionError) as raised:
        await restore(rig, new, taken)
    assert only_reads(rig)
    assert_untouched(rig)
    assert new.psk not in error_text(raised.value)


@pytest.mark.parametrize(
    "address",
    ["10.8.2.9", "10.9.1.9", "10.8.0.255", "10.8.2.0", "192.168.1.9", "0.0.0.0", "127.0.0.1", "255.255.255.255"],
)
async def test_restore_peer_on_an_address_outside_the_subnet_is_a_value_error_and_writes_nothing(
    rig: Rig, new: NewClient, address: str
) -> None:
    with pytest.raises(ValueError):
        await restore(rig, new, address)
    assert only_reads(rig)
    assert_untouched(rig)


@pytest.mark.parametrize("address", ["10.8.1.0", "10.8.1.255"], ids=["network-and-interface", "broadcast"])
async def test_restore_peer_refuses_the_network_and_broadcast_addresses(rig: Rig, new: NewClient, address: str) -> None:
    with pytest.raises((ValueError, CommandExecutionError)):
        await restore(rig, new, address)
    assert only_reads(rig)
    assert_untouched(rig)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "not-an-ip",
        "10.8.1",
        "10.8.1.300",
        "10.8.1.09",
        " 10.8.1.9",
        "10.8.1.9 ",
        "10.8.1.9\n",
        "10.8.1.9/32",
        "10.8.1.9/24",
        "10.8.1.9,10.8.1.10",
        "10.8.1.9\nAllowedIPs = 0.0.0.0/0",
        "0.0.0.0/0",
        "::1",
        "::ffff:10.8.1.9",
        "fd00::9",
    ],
)
async def test_restore_peer_with_a_malformed_address_is_a_value_error_with_no_commands(
    rig: Rig, new: NewClient, address: str
) -> None:
    with pytest.raises(ValueError):
        await restore(rig, new, address)
    assert rig.fake.commands == []
    assert_untouched(rig)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "A" * 43,
        "A" * 44,
        "A" * 42 + "=",
        "{key}\n",
        "{key}\n[Peer]\nPublicKey = {key}",
        "{key}\nAllowedIPs = 0.0.0.0/0",
        "../../etc/passwd",
        "$(reboot)=",
    ],
    ids=[
        "empty",
        "short",
        "no-equals",
        "44",
        "42",
        "newline",
        "peer-injection",
        "allowedips-injection",
        "path",
        "subshell",
    ],
)
@pytest.mark.parametrize("field", ["public_key", "psk"])
async def test_restore_peer_with_a_malformed_key_or_psk_is_a_value_error_with_no_commands(
    rig: Rig, new: NewClient, field: str, bad: str
) -> None:
    value = bad.format(key=new.key)
    client = NewClient(value if field == "public_key" else new.key, value if field == "psk" else new.psk)
    with pytest.raises(ValueError) as raised:
        await restore(rig, client)
    assert rig.fake.commands == []
    assert_untouched(rig)
    if field == "public_key":
        assert new.psk not in error_text(raised.value)  # годный psk в тексте ошибки не нужен


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a b",
        "x" * 33,
        "Phone\n[Peer]",
        "Phone\n",
        "$(reboot)",
        "x; rm -rf /",
        "имя",
        "a/b",
        "../x",
        "a\x00b",
        "a=b",
    ],
)
async def test_restore_peer_with_a_bad_name_is_a_value_error_with_no_commands(
    rig: Rig, new: NewClient, name: str
) -> None:
    with pytest.raises(ValueError):
        await restore(rig, new, name=name)
    assert rig.fake.commands == []
    assert_untouched(rig)


@pytest.mark.parametrize("name", ["a", "x" * 32, "Phone_1-x"])
async def test_restore_peer_accepts_names_of_the_allowed_alphabet_and_length(
    rig: Rig, new: NewClient, name: str
) -> None:
    assert await restore(rig, new, name=name) is True
    assert rig.table()[-1]["userData"]["clientName"] == name


async def test_restore_peer_never_sends_the_client_name_or_keys_in_commands(rig: Rig, new: NewClient) -> None:
    await restore(rig, new)
    text = "\n".join(rig.fake.commands) + "\n".join(rig.fake.writes)
    for value in (NEW_NAME, new.key, new.psk, NEW_ADDRESS):
        assert value not in text


# --- гонка с самим приложением -------------------------------------------------------------------------------------


def changes(rig: Rig) -> dict[str, Callable[[str], str]]:
    other = generate_secret_keys()
    return {
        "app-added-a-peer": lambda text: text + "\n" + new_block(NewClient(other.public_key, other.psk), "10.8.1.77"),
        "trailing-newline": lambda text: text + "\n",
        "obfuscation-edit": lambda text: text.replace("Jc = 5", "Jc = 7"),
        "peer-removed": lambda text: text.replace(rig.blocks[rig.c], "").replace("\n\n\n", "\n\n"),
    }


@pytest.mark.parametrize("change", ["app-added-a-peer", "trailing-newline", "obfuscation-edit", "peer-removed"])
@pytest.mark.parametrize("operation", ["revoke", "restore"])
async def test_config_changed_by_the_app_between_reads_is_an_error_and_nothing_is_written(
    rig: Rig, new: NewClient, operation: str, change: str
) -> None:
    changed = changes(rig)[change](rig.original)
    assert changed != rig.original

    def app_edits_the_file(path: str, number: int) -> None:
        if path == rig.app.config_path and number >= 2:
            rig.app.files[path] = changed

    rig.app.on_read = app_edits_the_file
    with pytest.raises(config_changed_error()) as raised:
        if operation == "revoke":
            await revoke(rig, rig.b)
        else:
            await restore(rig, new)
    assert rig.app.files[rig.app.config_path] == changed  # чужая правка не затёрта
    assert rig.app.files[APP_CLIENTS_TABLE] == rig.original_table
    assert only_reads(rig) and rig.fake.writes == [] and rig.fake.copies == [] and rig.app.applies == 0
    assert rig.fake.stage_files() == []
    text = error_text(raised.value)
    assert not any(secret in text for secret in (*rig.secrets, new.psk)) and "PrivateKey" not in text


async def test_unchanged_config_between_reads_does_not_trip_the_guard(rig: Rig) -> None:
    rig.app.on_read = lambda path, number: None
    await revoke(rig, rig.b)
    assert rig.app.files[rig.app.config_path] == rig.config(rig.b)


# --- сбои посреди операции -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["revoke", "restore"])
async def test_failed_apply_rolls_the_config_back_and_leaves_nothing_behind(
    rig: Rig, new: NewClient, operation: str
) -> None:
    rig.app.apply_fails = True
    with pytest.raises(CommandExecutionError) as raised:
        if operation == "revoke":
            await revoke(rig, rig.b)
        else:
            await restore(rig, new)
    assert rig.app.files[rig.app.config_path] == rig.original  # в контейнере исходный конфиг
    assert rig.app.files[APP_CLIENTS_TABLE] == rig.original_table  # таблицу не трогали
    assert rig.live_keys() == [rig.a, rig.b, rig.c, rig.d] and rig.app.applies == 0
    assert table_commands(rig) == []
    new_text = rig.config(rig.b) if operation == "revoke" else rig.original + "\n" + new_block(new)
    assert [copy.content for copy in rig.fake.copies] == [new_text, rig.original]  # правка, затем откат тем же способом
    assert rig.fake.writes == [stage_conf(rig.container)] * 2
    assert normalized(rig.fake).count(f"rm -f {stage_conf(rig.container)}") == 2
    text = error_text(raised.value)
    assert not any(secret in text for secret in (*rig.secrets, new.psk))
    assert not any(marker in text for marker in ("PrivateKey", "PresharedKey", "[Peer]", "[Interface]"))
    assert_clean(rig, (new.psk,))


async def test_failed_apply_and_failed_rollback_still_cleans_the_host_and_hides_secrets(
    rig: Rig, new: NewClient
) -> None:
    rig.app.apply_fails = True
    rig.app.on_apply = lambda: rig.app.docker_cp_fails.add(rig.app.config_path)  # откат тоже не удастся
    with pytest.raises(CommandExecutionError) as raised:
        await restore(rig, new)
    text = error_text(raised.value)
    assert not any(secret in text for secret in (*rig.secrets, new.psk)) and "PrivateKey" not in text
    assert rig.app.applies == 0 and table_commands(rig) == []
    assert_clean(rig, (new.psk,))


@pytest.mark.parametrize("operation", ["revoke", "restore"])
async def test_failed_docker_cp_leaves_the_config_unchanged_and_the_stage_empty(
    rig: Rig, new: NewClient, operation: str
) -> None:
    rig.app.docker_cp_fails.add(rig.app.config_path)
    with pytest.raises(CommandExecutionError) as raised:
        if operation == "revoke":
            await revoke(rig, rig.b)
        else:
            await restore(rig, new)
    assert_container_untouched(rig)
    assert syncconf_command(rig.app) not in normalized(rig.fake)  # не применяли то, что не записалось
    assert table_commands(rig) == []
    assert normalized(rig.fake).count(f"rm -f {stage_conf(rig.container)}") == 1  # промежуточный файл убран
    text = error_text(raised.value)
    assert not any(secret in text for secret in (*rig.secrets, new.psk)) and "PrivateKey" not in text
    assert rig.fake.writes == [stage_conf(rig.container)]
    assert_clean(rig, (new.psk,))


async def test_failed_sftp_write_of_the_stage_file_changes_nothing(rig: Rig) -> None:
    rig.fake.write_fails.add(stage_conf(rig.container))
    with pytest.raises((SSHServerError, SSHConnectionError)) as raised:
        await revoke(rig, rig.b)
    assert not isinstance(raised.value, UnsupportedOperationError)
    assert rig.fake.write_attempts == [stage_conf(rig.container)]  # до SFTP-записи дошли, дальше не пошли
    assert rig.app.files[rig.app.config_path] == rig.original and rig.app.applies == 0
    assert not any(c.startswith("docker cp") for c in normalized(rig.fake))
    assert_clean(rig)


async def test_failed_table_copy_after_apply_is_not_an_error_and_the_config_stays_changed(rig: Rig) -> None:
    rig.app.docker_cp_fails.add(APP_CLIENTS_TABLE)
    assert await revoke(rig, rig.b) is None
    assert rig.app.files[rig.app.config_path] == rig.config(rig.b) and rig.live_keys() == [rig.a, rig.c, rig.d]
    assert rig.app.files[APP_CLIENTS_TABLE] == rig.original_table  # таблица осталась прежней
    assert_clean(rig)


async def test_failed_table_stage_write_after_apply_is_not_an_error(rig: Rig, new: NewClient) -> None:
    rig.fake.write_fails.add(stage_table(rig.container))
    assert await restore(rig, new) is True
    assert rig.app.files[rig.app.config_path] == rig.original + "\n" + new_block(new)
    assert rig.app.files[APP_CLIENTS_TABLE] == rig.original_table
    assert_clean(rig, (new.psk,))


async def test_leftover_stage_files_from_a_crashed_run_are_replaced_and_removed(rig: Rig) -> None:
    rig.fake.files[stage_conf(rig.container)] = "[Interface]\nPrivateKey = from-a-crashed-run\n"
    rig.fake.files[stage_table(rig.container)] = "garbage"
    await revoke(rig, rig.b)
    assert rig.app.files[rig.app.config_path] == rig.config(rig.b)  # копировали свежий файл, а не остаток
    assert rig.fake.stage_files() == []


# --- clientsTable: «доступ важнее таблицы» -------------------------------------------------------------------------


def odd_tables(rig: Rig) -> dict[str, str | None]:
    return {
        "not-json": "this is not json",
        "empty": "",
        "cut": '[{"clientId": ',
        "null": "null",
        "legacy-object": clients_table_json({rig.a: "Alice", rig.b: "Bob"}, legacy=True),
        "object-with-list": '{"clients": []}',
        "missing": None,
    }


@pytest.mark.parametrize("kind", ["not-json", "empty", "cut", "null", "legacy-object", "object-with-list", "missing"])
@pytest.mark.parametrize("operation", ["revoke", "restore"])
async def test_a_clients_table_that_is_not_a_json_array_is_left_alone_and_the_operation_succeeds(
    rig: Rig, new: NewClient, operation: str, kind: str
) -> None:
    table = odd_tables(rig)[kind]
    if table is None:
        del rig.app.files[APP_CLIENTS_TABLE]
    else:
        rig.app.files[APP_CLIENTS_TABLE] = table
    if operation == "revoke":
        await revoke(rig, rig.b)
        assert rig.app.files[rig.app.config_path] == rig.config(rig.b)
    else:
        assert await restore(rig, new) is True
        assert rig.app.files[rig.app.config_path] == rig.original + "\n" + new_block(new)
    assert rig.app.files.get(APP_CLIENTS_TABLE) == table  # байт в байт; отсутствующий файл не создан
    assert not any("docker cp" in c and "clientsTable" in c for c in normalized(rig.fake))
    assert stage_table(rig.container) not in rig.fake.writes
    assert rig.app.applies == 1
    assert_clean(rig, (new.psk,))


async def test_restore_peer_does_not_duplicate_or_rename_an_existing_table_entry(rig: Rig, new: NewClient) -> None:
    table = json.loads(rig.original_table)
    table.append({"clientId": new.key, "userData": {"clientName": "Chosen by the user"}})
    rig.app.files[APP_CLIENTS_TABLE] = json.dumps(table)
    assert await restore(rig, new) is True
    assert rig.table() == table  # имя, выбранное пользователем в приложении, не затёрто
    assert rig.app.files[rig.app.config_path] == rig.original + "\n" + new_block(new)


async def test_restore_peer_keeps_non_object_entries_of_the_table(rig: Rig, new: NewClient) -> None:
    table = [*json.loads(rig.original_table), "stray", 7, None]
    rig.app.files[APP_CLIENTS_TABLE] = json.dumps(table)
    await restore(rig, new)
    assert rig.table() == [*table, {"clientId": new.key, "userData": {"clientName": NEW_NAME}}]


# --- контейнер не годится ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["revoke", "restore"])
async def test_stopped_container_is_a_command_error_with_no_exec_and_no_start(
    rig: Rig, new: NewClient, operation: str
) -> None:
    rig.fake.containers[rig.container] = "exited"
    with pytest.raises(CommandExecutionError):
        if operation == "revoke":
            await revoke(rig, rig.b)
        else:
            await restore(rig, new)
    assert rig.fake.containers[rig.container] == "exited"
    assert not any(c.startswith(("docker exec", "docker cp", "docker start", "rm")) for c in rig.fake.commands)
    assert_untouched(rig)


@pytest.mark.parametrize("operation", ["revoke", "restore"])
async def test_missing_container_is_a_command_error_with_no_write(
    container: str, new: NewClient, operation: str
) -> None:
    fake = FakeServer()
    key = generate_secret_keys().public_key
    with pytest.raises(CommandExecutionError):
        if operation == "revoke":
            await call(fake, container, "revoke_client", key)
        else:
            await call(fake, container, "restore_peer", NEW_NAME, new.key, new.psk, NEW_ADDRESS)
    assert fake.changing_commands() == [] and fake.writes == [] and fake.files == {}
    assert not any(c.startswith("docker exec") for c in fake.commands)


@pytest.mark.parametrize("operation", ["revoke", "restore"])
async def test_panel_own_container_is_not_an_app_and_is_never_written(new: NewClient, operation: str) -> None:
    """`amnezia-awg` с папкой панели среди монтирований — контейнер панели: пишет в него только AmneziaWGProvider."""
    fake = FakeServer()
    async with FakeSSHClient(fake) as ssh:
        await AmneziaWGProvider(ssh).install()
    fake.commands.clear()
    files, writes = dict(fake.files), list(fake.writes)
    with pytest.raises(CommandExecutionError):
        if operation == "revoke":
            await call(fake, "amnezia-awg", "revoke_client", generate_secret_keys().public_key)
        else:
            await call(fake, "amnezia-awg", "restore_peer", NEW_NAME, new.key, new.psk, NEW_ADDRESS)
    assert fake.files == files and fake.writes == writes and fake.copies == []
    assert not any(c.startswith(("docker exec", "docker cp", "rm")) for c in fake.commands)
    assert fake.stage_files() == []


@pytest.mark.parametrize("operation", ["revoke", "restore"])
async def test_unreadable_config_is_a_command_error_and_nothing_is_written(
    rig: Rig, new: NewClient, operation: str
) -> None:
    del rig.app.files[rig.app.config_path]
    with pytest.raises(CommandExecutionError):
        if operation == "revoke":
            await revoke(rig, rig.b)
        else:
            await restore(rig, new)
    assert only_reads(rig) and rig.fake.writes == [] and rig.fake.copies == [] and rig.app.applies == 0
    assert rig.app.files[APP_CLIENTS_TABLE] == rig.original_table


async def test_failed_container_listing_is_an_error_with_no_write(rig: Rig) -> None:
    rig.fake.ps_fails = True
    with pytest.raises(CommandExecutionError):
        await revoke(rig, rig.b)
    assert only_reads(rig) and rig.fake.writes == []
    assert_untouched(rig)


# --- секреты -------------------------------------------------------------------------------------------------------


async def test_secrets_are_not_logged_by_a_successful_restore_and_revoke(
    rig: Rig, new: NewClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    result = await restore(rig, new)
    await revoke(rig, rig.b)
    for secret in (*rig.secrets, new.psk):
        assert secret not in caplog.text
        assert secret not in repr(result)
    assert "PrivateKey" not in caplog.text and "PresharedKey" not in caplog.text


@pytest.mark.parametrize("failure", ["apply", "docker-cp", "changed"])
async def test_secrets_are_not_logged_or_quoted_when_the_operation_fails(
    rig: Rig, new: NewClient, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    caplog.set_level(logging.DEBUG)
    if failure == "apply":
        rig.app.apply_fails = True
    elif failure == "docker-cp":
        rig.app.docker_cp_fails.add(rig.app.config_path)
    else:

        def app_edits_the_file(path: str, number: int) -> None:
            if number >= 2:
                rig.app.files[path] = rig.original + "\n"

        rig.app.on_read = app_edits_the_file
    with pytest.raises(CommandExecutionError) as raised:
        await restore(rig, new)
    texts = [caplog.text, error_text(raised.value)]
    for secret in (*rig.secrets, new.psk):
        assert not any(secret in text for text in texts)
    assert not any(marker in text for text in texts for marker in ("PrivateKey", "PresharedKey"))
    assert_clean(rig, (new.psk,))


async def test_revoke_leaves_no_secret_on_the_host_even_though_the_stage_file_held_the_server_key(rig: Rig) -> None:
    await revoke(rig, rig.b)
    (conf_copy, *_) = rig.fake.copies
    assert rig.app.private_key in conf_copy.content  # файл на хосте действительно был секретным ...
    assert rig.fake.stage_files() == []  # ... поэтому после операции его нет
    assert not any(rig.app.private_key in text for text in rig.fake.files.values())


# --- ревью 23.3: расхождение файла и интерфейса, откат, остатки stage ---------------------------------------------


async def test_revoke_heals_a_peer_that_is_in_the_interface_but_already_gone_from_the_file(rig: Rig) -> None:
    """Прошлый отзыв записал файл, но оборвался до применения: повтор должен снять peer и с живого интерфейса."""
    rig.app.files[rig.app.config_path] = rig.config(rig.b)  # в файле b уже нет, на интерфейсе он ещё жив
    assert rig.b in rig.live_keys()
    await revoke(rig, rig.b)
    assert rig.b not in rig.live_keys()
    assert_clean(rig)


async def test_restore_peer_heals_a_peer_that_is_in_the_file_but_missing_from_the_interface(
    rig: Rig, new: NewClient
) -> None:
    assert await restore(rig, new) is True
    rig.app.peers = [p for p in rig.app.peers if p.public_key != new.key]  # файл новый, интерфейс старый
    assert await restore(rig, new) is False  # в файле peer уже есть...
    assert new.key in rig.live_keys()  # ...но интерфейс выровнен по файлу
    assert_clean(rig)


async def test_failed_apply_rolls_back_and_realigns_the_interface(rig: Rig) -> None:
    calls = {"n": 0}

    def fail_first_apply_only() -> None:
        calls["n"] += 1
        rig.app.apply_fails = calls["n"] == 1

    rig.app.on_apply = fail_first_apply_only
    with pytest.raises(CommandExecutionError):
        await revoke(rig, rig.b)
    assert rig.app.files[rig.app.config_path] == rig.original  # файл откачен
    assert rig.live_keys() == [rig.a, rig.b, rig.c, rig.d]  # интерфейс совпадает с файлом
    assert calls["n"] == 2  # откат применён повторно
    assert_clean(rig)


async def test_rollback_does_not_overwrite_a_change_the_app_made_during_the_failed_apply(rig: Rig) -> None:
    """Пока применение падало, приложение Amnezia дописало клиента: откат не должен затереть его правку."""
    appended = (
        rig.original + "\n[Peer]\nPublicKey = " + generate_secret_keys().public_key + "\nAllowedIPs = 10.8.1.99/32\n"
    )

    def app_edits_the_file() -> None:
        rig.app.files[rig.app.config_path] = appended
        rig.app.apply_fails = True

    rig.app.on_apply = app_edits_the_file
    with pytest.raises(CommandExecutionError):
        await revoke(rig, rig.b)
    assert rig.app.files[rig.app.config_path] == appended  # чужая правка на месте, мы её не откатывали
    assert_clean(rig)


async def test_stage_leftovers_of_an_interrupted_operation_are_removed_before_writing(rig: Rig) -> None:
    rig.fake.files[f"{STAGE_DIR}/{rig.container}.conf.tmp-deadbeef"] = "[Interface]\nPrivateKey = LEFTOVER\n"
    await revoke(rig, rig.b)
    assert rig.fake.stage_files() == []
    assert f"rm -f {STAGE_DIR}/{rig.container}.*" in normalized(rig.fake)
    assert STAGE_DIR in rig.fake.dirs and rig.fake.dirs[STAGE_DIR] == 0o700  # закрытый каталог, а не по umask
