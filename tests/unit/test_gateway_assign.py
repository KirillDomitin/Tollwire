"""Назначение выходов клиентам шлюза (этап 24.3): балансировщик, assign.rules, правила
метки, start.sh, apply_assignments с откатом. Действие iptables/ip rule - только живым прогоном (см.
src/infrastructure/vpn/CLAUDE.md, раздел «Выходы шлюза»)."""

import re
from ipaddress import IPv4Address
from uuid import uuid4

import pytest

from core.ssh_exception import CommandExecutionError
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import (
    ASSIGN_CHAIN,
    EXIT_RULE_PRIORITY,
    GATEWAY_LAYOUT,
    KILL_SWITCH_PRIORITY,
    RU_RULE_PRIORITY,
    assign_apply_command,
    exit_mark,
    exit_needs_restart,
    parse_assign_chain,
    render_assign_rules,
    render_exit_config,
    render_start_script,
)
from modules.gateway.balancer import ExitCandidate, is_usable, pick_exit
from modules.gateway.models.gateway_exit import ExitState
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient
from tests.gateway_support import apply_exits, exit_tunnel
from tests.unit.test_gateway_exits import CLIENT_CONFIG, tunnel

NAME = GATEWAY_LAYOUT.container_name
HOST_DIR = GATEWAY_LAYOUT.host_config_dir
ASSIGN_FILE = f"{HOST_DIR}/assign.rules"


def ip(last: int) -> IPv4Address:
    return IPv4Address(f"10.77.0.{last}")


# --- балансировщик ------------------------------------------------------------------------------------------------


def candidate(
    slot: int,
    *,
    assigned: int = 0,
    weight: int = 1,
    max_clients: int | None = None,
    state: ExitState = ExitState.ACTIVE,
    health: str | None = "ok",
) -> ExitCandidate:
    return ExitCandidate(uuid_for(slot), slot, weight, max_clients, assigned, state, health)


def uuid_for(slot: int):  # noqa: ANN201
    from uuid import UUID

    return UUID(int=slot)


def test_no_candidates_means_no_exit() -> None:
    assert pick_exit([]) is None


def test_the_least_loaded_exit_wins_and_the_tie_goes_to_the_smaller_slot() -> None:
    assert pick_exit([candidate(1, assigned=2), candidate(2, assigned=1)]).slot == 2  # type: ignore[union-attr]
    assert pick_exit([candidate(3), candidate(2), candidate(5)]).slot == 2  # type: ignore[union-attr]
    assert pick_exit([candidate(2, assigned=4), candidate(1, assigned=4)]).slot == 1  # type: ignore[union-attr]


def test_weights_scale_the_load() -> None:
    """(assigned + 1) / weight: выход с весом 3 принимает втрое больше клиентов."""
    exits = [candidate(1, weight=1), candidate(2, weight=3)]
    placed = {1: 0, 2: 0}
    for _ in range(8):
        chosen = pick_exit(exits)
        assert chosen is not None
        placed[chosen.slot] += 1
        exits = [candidate(c.slot, weight=c.weight, assigned=c.assigned + (c.slot == chosen.slot)) for c in exits]
    assert placed == {1: 2, 2: 6}


def test_a_full_exit_is_skipped_and_when_all_are_full_there_is_none() -> None:
    assert pick_exit([candidate(1, assigned=2, max_clients=2), candidate(2, assigned=9)]).slot == 2  # type: ignore[union-attr]
    assert pick_exit([candidate(1, assigned=2, max_clients=2), candidate(2, assigned=5, max_clients=5)]) is None
    assert pick_exit([candidate(1, assigned=1, max_clients=2)]).slot == 1  # type: ignore[union-attr]


@pytest.mark.parametrize("health", ["down", "stale"])
def test_unhealthy_exits_get_no_new_clients(health: str) -> None:
    assert pick_exit([candidate(1, health=health), candidate(2, assigned=50)]).slot == 2  # type: ignore[union-attr]
    assert pick_exit([candidate(1, health=health)]) is None


@pytest.mark.parametrize("health", [None, "ok", "no_handshake"])
def test_exits_not_yet_checked_or_just_raised_are_usable(health: str | None) -> None:
    assert is_usable(candidate(1, health=health))


@pytest.mark.parametrize("state", [s for s in ExitState if s != ExitState.ACTIVE])
def test_only_active_exits_are_usable(state: ExitState) -> None:
    assert pick_exit([candidate(1, state=state)]) is None


def test_the_choice_does_not_depend_on_the_order_of_candidates() -> None:
    exits = [candidate(slot, assigned=slot % 3, weight=1 + slot % 2) for slot in range(1, 9)]
    expected = pick_exit(exits)
    assert expected is not None
    for shift in range(len(exits)):
        assert pick_exit(exits[shift:] + exits[:shift]) == expected
        assert pick_exit(list(reversed(exits[shift:] + exits[:shift]))) == expected


# --- assign.rules ---------------------------------------------------------------------------------------------------


def test_assign_rules_are_exactly_this() -> None:
    text = render_assign_rules({ip(3): 2, ip(2): 1})
    assert text == (
        "*mangle\n:GW_ASSIGN - [0:0]\n"
        "-A GW_ASSIGN -s 10.77.0.2/32 -j MARK --set-mark 1001\n"
        "-A GW_ASSIGN -s 10.77.0.3/32 -j MARK --set-mark 1002\n"
        "COMMIT\n"
    )


def test_an_empty_map_is_a_valid_file_that_empties_the_chain() -> None:
    assert render_assign_rules({}) == "*mangle\n:GW_ASSIGN - [0:0]\nCOMMIT\n"


def test_the_order_of_the_map_does_not_change_the_text() -> None:
    forward = {ip(n): 1 + n % 3 for n in range(2, 30)}
    backward = dict(reversed(list(forward.items())))
    assert render_assign_rules(forward) == render_assign_rules(backward)


@pytest.mark.parametrize(
    "bad_address", ["10.77.0.2", "10.77.0.2; reboot", None, 3232235777, IPv4Address("10.8.1.2"), IPv4Address("8.8.8.8")]
)
def test_only_addresses_of_the_client_subnet_get_into_the_file(bad_address: object) -> None:
    with pytest.raises(ValueError):
        render_assign_rules({bad_address: 1})  # type: ignore[dict-item]


@pytest.mark.parametrize("bad_slot", [0, 201, True, "1", None, 1.0])
def test_only_integer_slots_get_into_the_file(bad_slot: object) -> None:
    with pytest.raises(ValueError):
        render_assign_rules({ip(2): bad_slot})  # type: ignore[dict-item]


def test_no_user_text_reaches_the_file() -> None:
    text = render_assign_rules({ip(2): 4})
    for line in text.splitlines():
        assert re.fullmatch(
            r"\*mangle|:GW_ASSIGN - \[0:0\]|COMMIT|-A GW_ASSIGN -s 10\.77\.0\.\d+/32 -j MARK --set-mark \d+", line
        )


def test_the_chain_output_of_iptables_is_parsed_back() -> None:
    output = (
        "-N GW_ASSIGN\n"
        "-A GW_ASSIGN -s 10.77.0.2/32 -j MARK --set-xmark 0x3e9/0xffffffff\n"
        "-A GW_ASSIGN -s 10.77.0.3/32 -j MARK --set-mark 1002\n"
        "-A GW_ASSIGN -s 10.77.0.4 -j MARK --set-mark 0x3ea\n"
    )
    assert parse_assign_chain(output) == {ip(2): 1, ip(3): 2, ip(4): 2}


def test_parsing_skips_garbage_and_marks_outside_the_slots() -> None:
    output = (
        "-A GW_ASSIGN -s 10.77.0.2/32 -j MARK --set-mark 5\n"  # метка не выхода
        "-A GW_ASSIGN -s 10.77.0.999/32 -j MARK --set-mark 1001\n"
        "-A GW_ASSIGN -s 10.77.0.5/32 -j ACCEPT\n"
        "-A OTHER -s 10.77.0.6/32 -j MARK --set-mark 1001\n"
        "garbage\n"
    )
    assert parse_assign_chain(output) == {}


def test_render_and_parse_agree_for_every_slot() -> None:
    mapping = {ip(2 + slot): slot for slot in range(1, 201)}
    chain = "\n".join(
        f"-A GW_ASSIGN -s {address}/32 -j MARK --set-xmark 0x{exit_mark(slot):x}/0xffffffff"
        for address, slot in mapping.items()
    )
    assert parse_assign_chain(chain) == mapping


def test_the_apply_command_is_test_then_apply_of_the_same_file() -> None:
    assert assign_apply_command(GATEWAY_LAYOUT) == (
        f"docker exec {NAME} sh -c 'iptables-restore -w --test --noflush < /etc/vpn-admin/gw/assign.rules"
        " && iptables-restore -w --noflush < /etc/vpn-admin/gw/assign.rules'"
    )


# --- правило метки и start.sh ---------------------------------------------------------------------------------------


def test_the_mark_rule_sits_between_the_russian_table_and_the_kill_switch() -> None:
    assert RU_RULE_PRIORITY < EXIT_RULE_PRIORITY < KILL_SWITCH_PRIORITY


def test_start_script_creates_the_chain_and_the_three_hooks_idempotently_before_the_interface() -> None:
    text = render_start_script(GATEWAY_LAYOUT)
    assert f"iptables -t mangle -N {ASSIGN_CHAIN} 2>/dev/null || true" in text
    hooks = [
        'ensure_ipt mangle PREROUTING -i "$GW_IF" -j CONNMARK --restore-mark',
        'ensure_ipt mangle PREROUTING -i "$GW_IF" -m mark --mark 0 -j GW_ASSIGN',
        'ensure_ipt mangle PREROUTING -i "$GW_IF" -j CONNMARK --save-mark',
    ]
    positions = [text.index(hook) for hook in hooks]
    assert positions == sorted(positions)  # восстановить метку -> назначить -> сохранить метку в соединении
    assert positions[-1] < text.index('awg-quick up "$GW_CONF"')  # правила стоят раньше подъёма клиентского интерфейса
    assert 'iptables-restore -w --noflush < "$GW_DIR/assign.rules"' in text
    assert 'if [ -f "$GW_DIR/assign.rules" ]; then' in text
    assert positions[-1] < text.index("iptables-restore -w --noflush")  # карта грузится после создания цепочки
    assert "nft " not in text and "conntrack " not in text.replace("-m conntrack ", "")  # только модуль, не утилита


def test_start_script_loads_the_map_in_both_modes_but_a_bad_file_does_not_stop_the_container() -> None:
    text = render_start_script(GATEWAY_LAYOUT)
    load = text.index("iptables-restore -w --noflush")
    assert load < text.index('if [ "$MODE" != apply ]; then')  # и run, и apply (до подъёма gw0)
    assert '|| echo "start.sh: assignments were not loaded" >&2' in text


def test_exit_config_gets_the_mark_rule_with_the_interface() -> None:
    config = render_exit_config(tunnel(slot=2))
    assert "PostUp = ip rule add priority 1000 fwmark 1002 lookup 1002 2>/dev/null || true" in config
    assert "PostDown = ip rule del priority 1000 fwmark 1002 lookup 1002 2>/dev/null || true" in config


def test_an_exit_raised_by_the_old_config_is_recreated() -> None:
    new = render_exit_config(tunnel())
    old = "\n".join(line for line in new.splitlines() if "ip rule" not in line) + "\n"  # конфиг 24.2
    assert exit_needs_restart(old, new) is True
    assert exit_needs_restart(new, new) is False
    assert (
        exit_needs_restart(new, render_exit_config(tunnel(config=CLIENT_CONFIG.replace("Jc = 5", "Jc = 9")))) is False
    )


# --- провайдер ------------------------------------------------------------------------------------------------------


async def installed() -> FakeServer:
    fake = FakeServer(layout=GATEWAY_LAYOUT)
    async with FakeSSHClient(fake) as ssh:
        await AmneziaGatewayProvider(ssh).install()
    return fake


async def call(fake: FakeServer, method: str, *args: object) -> object:
    async with FakeSSHClient(fake) as ssh:
        return await getattr(AmneziaGatewayProvider(ssh), method)(*args)


async def test_the_installed_gateway_has_the_chain_and_an_empty_map() -> None:
    fake = await installed()
    assert "assign" in fake.firewall
    assert await call(fake, "read_assignments") == {}


async def test_apply_writes_a_private_file_and_replaces_the_whole_chain() -> None:
    fake = await installed()
    await call(fake, "apply_assignments", {ip(2): 1, ip(3): 2})
    assert fake.files[ASSIGN_FILE] == render_assign_rules({ip(2): 1, ip(3): 2})
    assert fake.file_modes[ASSIGN_FILE] == 0o600
    assert fake.assign_chain == {"10.77.0.2": 1001, "10.77.0.3": 1002}
    assert await call(fake, "read_assignments") == {ip(2): 1, ip(3): 2}
    await call(fake, "apply_assignments", {ip(3): 1})  # полная замена, а не добавление
    assert fake.assign_chain == {"10.77.0.3": 1001}
    await call(fake, "apply_assignments", {})
    assert fake.assign_chain == {} and fake.files[ASSIGN_FILE].endswith(":GW_ASSIGN - [0:0]\nCOMMIT\n")


async def test_apply_commands_are_only_the_constant_ones() -> None:
    fake = await installed()
    start = len(fake.commands)
    await call(fake, "apply_assignments", {ip(2): 1})
    await call(fake, "read_assignments")
    allowed = re.compile(
        rf"docker ps -a --filter '[^']*' --format '\{{\{{\.State\}}\}}'"
        rf"|docker inspect --type container -f '[^']*' {NAME}"
        rf"|test -f {HOST_DIR}/assign\.rules"
        rf"|docker exec {NAME} iptables -t mangle -S PREROUTING"
        rf"|docker exec {NAME} iptables -t mangle -S GW_ASSIGN" + "|" + re.escape(assign_apply_command(GATEWAY_LAYOUT))
    )
    issued = [" ".join(c.split()) for c in fake.commands[start:]]
    assert all(allowed.fullmatch(command) for command in issued), issued


async def test_a_failed_test_keeps_the_chain_and_puts_the_previous_file_back() -> None:
    fake = await installed()
    await call(fake, "apply_assignments", {ip(2): 1})
    previous = fake.files[ASSIGN_FILE]
    fake.assign_test_fails = True
    with pytest.raises(CommandExecutionError, match="assignments"):
        await call(fake, "apply_assignments", {ip(2): 2, ip(3): 1})
    assert fake.files[ASSIGN_FILE] == previous  # файл на хосте не расходится с цепочкой
    assert fake.assign_chain == {"10.77.0.2": 1001}


async def test_a_failed_apply_with_no_previous_file_leaves_no_file() -> None:
    fake = await installed()
    fake.assign_apply_fails = True
    with pytest.raises(CommandExecutionError):
        await call(fake, "apply_assignments", {ip(2): 1})
    assert ASSIGN_FILE not in fake.files and fake.assign_chain == {}


async def test_a_failure_does_not_quote_the_command_output() -> None:
    fake = await installed()
    fake.assign_test_fails = True
    with pytest.raises(CommandExecutionError) as caught:
        await call(fake, "apply_assignments", {ip(2): 1})
    assert "line 3" not in str(caught.value) and "10.77.0.2" not in str(caught.value)


async def test_apply_refuses_foreign_addresses_before_touching_the_server() -> None:
    fake = await installed()
    start = len(fake.commands)
    with pytest.raises(ValueError):
        await call(fake, "apply_assignments", {IPv4Address("8.8.8.8"): 1})
    assert fake.commands[start:] == []


async def test_apply_needs_a_running_container() -> None:
    fake = await installed()
    fake.containers[NAME] = "exited"
    with pytest.raises(CommandExecutionError, match="not running"):
        await call(fake, "apply_assignments", {ip(2): 1})
    assert ASSIGN_FILE not in fake.files


async def test_in_sync_compares_both_the_chain_and_the_file() -> None:
    fake = await installed()
    desired = {ip(2): 1}
    await call(fake, "apply_assignments", desired)
    assert await call(fake, "assignments_in_sync", desired) is True
    assert await call(fake, "assignments_in_sync", {ip(2): 2}) is False
    fake.assign_chain["10.77.0.9"] = 1001  # лишнее правило в цепочке
    assert await call(fake, "assignments_in_sync", desired) is False
    fake.assign_chain = {"10.77.0.2": 1001}
    fake.files.pop(ASSIGN_FILE)  # цепочка верна, а после перезапуска карта пропала бы
    assert await call(fake, "assignments_in_sync", desired) is False


async def test_a_gateway_with_an_old_start_script_has_no_chain() -> None:
    fake = await installed()
    fake.firewall.discard("assign")
    with pytest.raises(CommandExecutionError, match="chain is missing"):
        await call(fake, "read_assignments")


async def test_the_map_survives_a_container_restart() -> None:
    fake = await installed()
    await call(fake, "apply_assignments", {ip(2): 1, ip(3): 2})
    fake.containers[NAME] = "exited"
    async with FakeSSHClient(fake) as ssh:
        assert (await ssh.execute(f"docker start {NAME}")).exit_status == 0
    assert fake.assign_chain == {"10.77.0.2": 1001, "10.77.0.3": 1002}  # start.sh загрузил assign.rules


async def test_the_mark_rule_follows_the_interface() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel(slot=1), await exit_tunnel(slot=2)])
    assert "1000:\tfrom all fwmark 0x3e9 lookup 1001" in fake.ip_rules
    assert "1000:\tfrom all fwmark 0x3ea lookup 1002" in fake.ip_rules
    await call(fake, "remove_exit", 1)
    assert "1000:\tfrom all fwmark 0x3e9 lookup 1001" not in fake.ip_rules
    assert "1000:\tfrom all fwmark 0x3ea lookup 1002" in fake.ip_rules


async def test_the_fake_never_creates_the_mark_rule_twice() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel(slot=1)])
    fake.exit_interfaces.clear()
    fake.exit_configs.clear()
    await apply_exits(fake, [await exit_tunnel(slot=1)])
    assert sum("fwmark" in rule for rule in fake.ip_rules) == 1


def test_uuid_helper_is_deterministic() -> None:
    assert uuid_for(7) == uuid_for(7) != uuid4()


# --- прыжки PREROUTING, готовность шлюза, защита FORWARD (ревью 24.3) -------------------------------------------

from core.ssh_exception import DockerError  # noqa: E402
from infrastructure.vpn.gateway_config import has_assign_hooks, missing_exit_rules  # noqa: E402

HEAD = "-A PREROUTING -i gw0"
GOOD_PREROUTING = (
    "-P PREROUTING ACCEPT\n"
    f"{HEAD} -j CONNMARK --restore-mark --nfmask 0xffffffff --ctmask 0xffffffff\n"
    f"{HEAD} -m mark --mark 0x0 -j GW_ASSIGN\n"
    f"{HEAD} -j CONNMARK --save-mark --nfmask 0xffffffff --ctmask 0xffffffff\n"
)


def test_the_three_hooks_are_recognised_only_all_together_and_in_order() -> None:
    assert has_assign_hooks(GOOD_PREROUTING, "gw0")
    lines = GOOD_PREROUTING.splitlines()
    assert not has_assign_hooks("-P PREROUTING ACCEPT\n", "gw0")  # цепочка могла быть создана restore'ом без прыжков
    assert not has_assign_hooks("\n".join([lines[0], lines[2], lines[3]]), "gw0")  # нет restore-mark
    assert not has_assign_hooks("\n".join([lines[0], lines[1], lines[3]]), "gw0")  # нет перехода в GW_ASSIGN
    assert not has_assign_hooks("\n".join([lines[0], lines[1], lines[2]]), "gw0")  # нет save-mark
    assert not has_assign_hooks("\n".join([lines[0], lines[2], lines[1], lines[3]]), "gw0")  # неверный порядок
    assert not has_assign_hooks(GOOD_PREROUTING, "other0")  # прыжки для другого интерфейса
    assert not has_assign_hooks(GOOD_PREROUTING.replace("--mark 0x0", "--mark 0x5"), "gw0")


def test_exit_mark_rules_are_looked_up_by_slot() -> None:
    rules = "0:\tfrom all lookup local\n1000:\tfrom all fwmark 0x3e9 lookup 1001\n9000:\tfrom 10.77.0.0/22 prohibit\n"
    assert missing_exit_rules(rules, [1, 2]) == [2]
    assert missing_exit_rules(rules, []) == []
    assert missing_exit_rules("1000:\tfrom all fwmark 0x3ea lookup 1001\n", [1]) == [1]  # метка и таблица не те


def test_start_script_blocks_connections_started_by_an_exit_idempotently() -> None:
    text = render_start_script(GATEWAY_LAYOUT)
    line = 'ensure_ipt filter FORWARD -i "gwx+" -m conntrack ! --ctstate ESTABLISHED,RELATED -j DROP'
    assert line in text
    assert text.index(line) < text.index('awg-quick up "$GW_CONF"')  # действует с первой секунды


async def test_a_fresh_gateway_has_no_assignment_problems_and_a_passing_health_check() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel(slot=1)])
    assert await call(fake, "assignment_problems") == []
    health = await call(fake, "health_check")
    check = next(item for item in health.checks if item.name == "assignments")  # type: ignore[attr-defined]
    assert check.ok


async def test_missing_hooks_or_exit_rules_are_reported_and_fail_health() -> None:
    fake = await installed()
    await apply_exits(fake, [await exit_tunnel(slot=1)])
    fake.firewall.discard("assign_hooks")
    fake.ip_rules = [rule for rule in fake.ip_rules if "fwmark" not in rule]
    problems = await call(fake, "assignment_problems")
    assert problems == ["PREROUTING hooks of the client assignments are missing", "ip rule of gwx1 is missing"]
    health = await call(fake, "health_check")
    check = next(item for item in health.checks if item.name == "assignments")  # type: ignore[attr-defined]
    assert not check.ok and "gwx1" in (check.detail or "")


async def test_apply_never_creates_the_chain_without_the_hooks() -> None:
    """restore сам создал бы GW_ASSIGN, а прыжков в неё нет: метки не ставились бы, клиенты остались бы без выхода."""
    fake = await installed()
    fake.firewall -= {"assign", "assign_hooks"}  # шлюз старого образца
    with pytest.raises(CommandExecutionError, match="hooks are missing"):
        await call(fake, "apply_assignments", {ip(2): 1})
    assert ASSIGN_FILE not in fake.files and "assign" not in fake.firewall and fake.assign_chain == {}


async def test_a_chain_without_hooks_is_not_in_sync_even_with_the_right_content() -> None:
    fake = await installed()
    await call(fake, "apply_assignments", {ip(2): 1})
    assert await call(fake, "assignments_in_sync", {ip(2): 1}) is True
    fake.firewall.discard("assign_hooks")
    assert await call(fake, "assignments_in_sync", {ip(2): 1}) is False


async def test_install_repairs_an_old_style_gateway_hooks_guard_and_sysctls() -> None:
    fake = await installed()
    fake.firewall -= {"assign", "assign_hooks", "ct_guard"}
    fake.container_sysctls[NAME] = [item.replace("rp_filter=0", "rp_filter=2") for item in fake.container_sysctls[NAME]]
    async with FakeSSHClient(fake) as ssh:
        provider = AmneziaGatewayProvider(ssh)
        await provider.install()
        assert provider.recreated == "sysctl_mismatch"
    assert {"assign", "assign_hooks", "ct_guard"} <= fake.firewall and await call(fake, "assignment_problems") == []


async def test_a_current_container_is_not_reported_as_recreated() -> None:
    fake = await installed()
    async with FakeSSHClient(fake) as ssh:
        provider = AmneziaGatewayProvider(ssh)
        await provider.install()
        assert provider.recreated is None


def outdated(fake: FakeServer) -> None:
    fake.container_sysctls[NAME] = [item.replace("rp_filter=0", "rp_filter=2") for item in fake.container_sysctls[NAME]]


async def test_recreation_retries_a_failed_start_once() -> None:
    fake = await installed()
    outdated(fake)
    fake.docker_run_fails = 1
    async with FakeSSHClient(fake) as ssh:
        provider = AmneziaGatewayProvider(ssh)
        await provider.install()
        assert provider.recreated == "sysctl_mismatch"
    assert fake.running and fake.docker_run_fails == 0


async def test_recreation_that_fails_twice_is_a_clear_error_and_the_missing_container_is_visible() -> None:
    fake = await installed()
    outdated(fake)
    fake.docker_run_fails = 2
    with pytest.raises(DockerError, match="could not be started again"):
        await call(fake, "install")
    assert NAME not in fake.containers  # контейнера нет - и это видно, а не скрыто
    health = await call(fake, "health_check")
    container = next(item for item in health.checks if item.name == "container")  # type: ignore[attr-defined]
    assert not container.ok and container.detail == "container not found"
