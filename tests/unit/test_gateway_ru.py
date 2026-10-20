"""Россия напрямую на шлюзе (этап 24.4): ru.prefixes, apply-ru.sh, apply_ru / ru_status на фейке, start.sh. Действие
скрипта на настоящем контейнере - живым прогоном (см. src/infrastructure/vpn/CLAUDE.md, раздел «Россия напрямую»)."""

import re
import shutil
import subprocess
from ipaddress import IPv4Network

import pytest

from core.ssh_exception import CommandExecutionError
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import (
    GATEWAY_LAYOUT,
    RU_TABLES,
    apply_ru_command,
    render_apply_ru_script,
    render_ru_file,
    render_ru_prefixes,
    render_start_script,
    ru_active_table,
    ru_applied_file,
    ru_digest,
    ru_routes_command,
)
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient

NAME = GATEWAY_LAYOUT.container_name
HOST_DIR = GATEWAY_LAYOUT.host_config_dir
LIST_FILE = f"{HOST_DIR}/ru.prefixes"
SCRIPT_FILE = f"{HOST_DIR}/apply-ru.sh"
A, B, C = IPv4Network("77.88.8.0/24"), IPv4Network("5.45.200.0/24"), IPv4Network("31.13.0.0/16")


def script() -> str:
    return render_apply_ru_script(GATEWAY_LAYOUT)


# --- ru.prefixes ------------------------------------------------------------------------------------------------


def test_the_prefix_file_is_cidr_per_line_sorted_and_unique() -> None:
    assert render_ru_prefixes([A, B, A]) == "5.45.200.0/24\n77.88.8.0/24\n"
    assert render_ru_prefixes([]) == ""


@pytest.mark.parametrize(
    "bad",
    ["10.0.0.0/8", "192.168.0.0/16", "172.17.0.0/16", "10.77.0.0/22", "127.0.0.0/8", "224.0.0.0/4", "0.0.0.0/0"],
)
def test_unsafe_networks_never_reach_the_file(bad: str) -> None:
    with pytest.raises(ValueError, match="public IPv4"):
        render_ru_prefixes([A, IPv4Network(bad)])


@pytest.mark.parametrize("bad", ["77.88.8.0/24", "2a00:1450::/32", None, 1234])
def test_only_network_objects_are_accepted(bad: object) -> None:
    from ipaddress import IPv6Network

    value: object = IPv6Network("2a00:1450::/32") if bad == "2a00:1450::/32" else bad
    with pytest.raises((ValueError, TypeError)):
        render_ru_prefixes([value])  # type: ignore[list-item]


def test_the_active_table_is_read_from_rule_100() -> None:
    rules = "0:\tfrom all lookup local\n100:\tfrom 10.77.0.0/22 lookup 201\n9000:\tfrom 10.77.0.0/22 prohibit\n"
    assert ru_active_table(rules, "10.77.0.0/22") == 201
    assert ru_active_table(rules.replace("201", "200"), "10.77.0.0/22") == 200
    assert ru_active_table(rules.replace("201", "202"), "10.77.0.0/22") is None  # не таблица ru
    assert ru_active_table(rules.replace("100:", "101:"), "10.77.0.0/22") is None
    assert ru_active_table(rules, "10.8.1.0/24") is None
    assert ru_active_table("", "10.77.0.0/22") is None


# --- apply-ru.sh -------------------------------------------------------------------------------------------------


def test_the_script_is_stable_text_without_placeholders() -> None:
    assert script() == render_apply_ru_script(GATEWAY_LAYOUT) and script().startswith("#!/bin/bash\n")
    assert not re.search(r"@[A-Z_]+@", script()), "unreplaced placeholder"
    assert "LIST=/etc/vpn-admin/gw/ru.prefixes" in script() and "SUBNET=10.77.0.0/22" in script()
    assert "TABLE_A=200" in script() and "TABLE_B=201" in script() and "PRIO=100" in script()


def test_the_script_uses_only_tools_that_exist_in_the_image() -> None:
    for forbidden in ("nft ", "ipset", "curl", "wget", "python", "perl", "xargs"):
        assert forbidden not in script(), forbidden


def test_the_script_fills_the_spare_table_and_never_leaves_a_moment_without_rule_100() -> None:
    text = script()
    fill, switch_add = text.index("ip -batch"), text.index('ip rule add priority "$PRIO"')
    switch_del = text.index('ip rule del priority "$PRIO" from "$SUBNET" lookup "$active"')
    flush_old = text.index('ip route flush table "$active"')
    assert fill < switch_add < switch_del < flush_old  # новое правило - до удаления старого, очистка старой - в конце


def test_flushing_an_empty_old_table_is_not_a_failure() -> None:
    """Живая проверка: у пустой таблицы нет FIB, `ip route flush` отвечает кодом 2 - при `set -e` скрипт падал бы после
    переключения правила, а apply_ru откатывал бы файл списка."""
    text = script()
    flush_old = text[text.index('ip route flush table "$active"') :].splitlines()[0]
    assert flush_old.endswith("2>/dev/null || true")


def test_a_failed_batch_clears_the_spare_table_and_exits_before_touching_rule_100() -> None:
    text = script()
    block = text[text.index("if ! ip -batch") : text.index("# Switch")]
    assert 'ip route flush table "$spare"' in block and "fail " in block
    assert "ip rule" not in block  # активное правило и активная таблица не затронуты


def test_the_script_checks_every_line_and_the_default_gateway() -> None:
    text = script()
    assert "[0-9]{1,3}(\\.[0-9]{1,3}){3}/[0-9]{1,2}" in text
    assert "no default gateway in the container" in text and "ip route show default" in text
    assert "set -eu" in text and "trap 'rm -f \"$batch\"' EXIT" in text


def test_the_script_takes_no_arguments_and_the_command_is_constant() -> None:
    """Список читается из файла, строки проверяются; аргументов командной строки скрипт не использует."""
    assert "$@" not in script() and "${1" not in script() and '"$1"' not in script()
    assert apply_ru_command(GATEWAY_LAYOUT) == f"docker exec {NAME} bash /etc/vpn-admin/gw/apply-ru.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_the_script_is_valid_bash() -> None:
    result = subprocess.run(["bash", "-n"], input=script().encode(), capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


def test_start_sh_loads_the_list_only_when_the_container_starts() -> None:
    text = render_start_script(GATEWAY_LAYOUT)
    assert 'if [ "$MODE" != apply ] && [ -f "$GW_DIR/ru.prefixes" ] && [ -f "$GW_DIR/apply-ru.sh" ]; then' in text
    assert 'bash "$GW_DIR/apply-ru.sh" || echo "start.sh: Russian routes were not loaded" >&2' in text
    assert text.index("apply-ru.sh") < text.index("ensure_rule 9000")  # маршруты - до kill-switch и подъёма gw0
    assert "lookup (200|201|ru)" in text  # правило 100 может указывать на любую из двух таблиц


# --- провайдер на фейке ---------------------------------------------------------------------------------------------


async def installed() -> FakeServer:
    fake = FakeServer(layout=GATEWAY_LAYOUT)
    async with FakeSSHClient(fake) as ssh:
        await AmneziaGatewayProvider(ssh).install()
    return fake


async def call(fake: FakeServer, method: str, *args: object) -> object:
    async with FakeSSHClient(fake) as ssh:
        return await getattr(AmneziaGatewayProvider(ssh), method)(*args)


async def test_the_first_apply_switches_from_table_200_to_201_and_the_second_back() -> None:
    fake = await installed()
    assert "100:\tfrom 10.77.0.0/22 lookup 200" in fake.ip_rules and (await call(fake, "ru_status")).routes == 0  # type: ignore[attr-defined]

    first = await call(fake, "apply_ru", [A, B])
    assert (first.table, first.routes) == (201, 2)  # type: ignore[attr-defined]
    assert fake.ru_tables == {201: ["5.45.200.0/24 via 172.17.0.1 dev eth0", "77.88.8.0/24 via 172.17.0.1 dev eth0"]}
    assert sum(rule.startswith("100:") for rule in fake.ip_rules) == 1  # старое правило снято, новое одно

    second = await call(fake, "apply_ru", [A, B, C])
    assert (second.table, second.routes) == (200, 3)  # type: ignore[attr-defined]
    assert list(fake.ru_tables) == [200]  # старая таблица очищена
    assert fake.ru_applies == 2


async def test_the_file_and_the_script_are_written_privately_and_commands_are_constant() -> None:
    fake = await installed()
    start = len(fake.commands)
    await call(fake, "apply_ru", [A])
    assert fake.files[LIST_FILE] == render_ru_file([A])[0] and fake.files[SCRIPT_FILE] == script()
    assert fake.file_modes[LIST_FILE] == fake.file_modes[SCRIPT_FILE] == 0o600
    allowed = re.compile(
        rf"docker ps -a --filter '[^']*' --format '\{{\{{\.State\}}\}}'"
        rf"|docker inspect --type container -f '[^']*' {NAME}"
        rf"|{re.escape(apply_ru_command(GATEWAY_LAYOUT))}"
        rf"|docker exec {NAME} ip rule show"
        rf"|{re.escape(ru_routes_command(GATEWAY_LAYOUT, 200))}"
        rf"|{re.escape(ru_routes_command(GATEWAY_LAYOUT, 201))}"
    )
    issued = [" ".join(command.split()) for command in fake.commands[start:]]
    assert all(allowed.fullmatch(command) for command in issued), issued


async def test_the_status_counts_routes_of_the_active_table_only() -> None:
    fake = await installed()
    await call(fake, "apply_ru", [A, B])
    fake.ru_tables[200] = ["31.13.0.0/16 via 172.17.0.1 dev eth0"]  # мусор в запасной таблице не считается
    status = await call(fake, "ru_status")
    assert (status.table, status.routes) == (201, 2)  # type: ignore[attr-defined]


async def test_a_failed_apply_leaves_the_working_table_and_puts_the_previous_file_back() -> None:
    fake = await installed()
    await call(fake, "apply_ru", [A, B])
    tables, rules, previous = dict(fake.ru_tables), list(fake.ip_rules), fake.files[LIST_FILE]
    fake.ru_apply_fails = True
    with pytest.raises(CommandExecutionError, match="Failed to apply the Russian routes"):
        await call(fake, "apply_ru", [A, B, C])
    assert fake.ru_tables == tables and fake.ip_rules == rules  # активная таблица цела
    assert fake.files[LIST_FILE] == previous  # перезапуск загрузит тот же список, что работает


async def test_a_failed_first_apply_leaves_no_list_file() -> None:
    fake = await installed()
    fake.ru_apply_fails = True
    with pytest.raises(CommandExecutionError):
        await call(fake, "apply_ru", [A])
    assert LIST_FILE not in fake.files and fake.ru_tables == {}


async def test_a_broken_line_that_bypassed_validation_does_not_break_the_active_table() -> None:
    """Подложенная в обход проверки строка: `ip -batch` падает, скрипт чистит запасную, активная работает."""
    fake = await installed()
    await call(fake, "apply_ru", [A, B])
    tables = dict(fake.ru_tables)
    fake.files[LIST_FILE] = "77.88.8.0/24\n300.1.1.0/24\n"
    async with FakeSSHClient(fake) as ssh:
        result = await ssh.execute(apply_ru_command(GATEWAY_LAYOUT))
    assert result.exit_status != 0 and fake.ru_tables == tables


async def test_unsafe_or_empty_lists_are_refused_before_the_server_is_touched() -> None:
    fake = await installed()
    start = len(fake.commands)
    for bad in ([IPv4Network("10.0.0.0/8")], [A, IPv4Network("224.0.0.0/4")], []):
        with pytest.raises(ValueError):
            await call(fake, "apply_ru", bad)
    assert fake.commands[start:] == [] and LIST_FILE not in fake.files


async def test_apply_needs_a_running_container() -> None:
    fake = await installed()
    fake.containers[NAME] = "exited"
    with pytest.raises(CommandExecutionError, match="not running"):
        await call(fake, "apply_ru", [A])
    assert LIST_FILE not in fake.files


async def test_the_list_returns_after_a_container_restart_from_the_file() -> None:
    fake = await installed()
    await call(fake, "apply_ru", [A, B])
    fake.containers[NAME] = "exited"
    async with FakeSSHClient(fake) as ssh:
        await ssh.execute(f"docker start {NAME}")
    status = await call(fake, "ru_status")
    assert status.routes == 2 and sum(rule.startswith("100:") for rule in fake.ip_rules) == 1  # type: ignore[attr-defined]


async def test_repairing_a_running_gateway_does_not_switch_the_tables() -> None:
    fake = await installed()
    await call(fake, "apply_ru", [A, B])
    applies = fake.ru_applies
    await call(fake, "install")
    await call(fake, "install")
    assert fake.ru_applies == applies  # install() идёт в режиме apply: таблицы не переключаются
    assert fake.files[SCRIPT_FILE] == script()


async def test_a_stale_script_is_rewritten_on_install() -> None:
    fake = await installed()
    fake.files[SCRIPT_FILE] = "#!/bin/bash\nexit 1\n"
    await call(fake, "install")
    assert fake.files[SCRIPT_FILE] == script()


def test_the_two_tables_are_200_and_201() -> None:
    assert RU_TABLES == (200, 201)
    with pytest.raises(ValueError):
        ru_routes_command(GATEWAY_LAYOUT, 254)


# --- зарезервированные диапазоны на уровне шлюза (общий источник правды: split_routes.py) ------------------------


@pytest.mark.parametrize(
    "bad",
    ["172.0.0.0/8", "192.0.0.0/8", "169.0.0.0/8", "100.0.0.0/8", "172.0.0.0/9", "169.192.0.0/10", "198.18.0.0/15"],
)
def test_networks_overlapping_reserved_ranges_never_reach_the_file(bad: str) -> None:
    with pytest.raises(ValueError, match="public IPv4"):
        render_ru_prefixes([A, IPv4Network(bad)])


def test_the_gateway_and_the_list_use_one_source_of_truth() -> None:
    from infrastructure.vpn import gateway_config, split_routes
    from modules.geoip import parser

    assert gateway_config.is_routable is split_routes.is_routable
    assert parser.is_routable is split_routes.is_routable
    assert parser.subtract_reserved is split_routes.subtract_reserved


# --- отпечаток списка, ru.applied, flock -----------------------------------------------------------------------------


def test_the_file_starts_with_the_digest_of_the_prefixes_below_it() -> None:
    text, digest = render_ru_file([B, A])
    body = render_ru_prefixes([A, B])
    assert text == f"# sha256={digest}\n{body}" and digest == ru_digest(body)
    assert render_ru_file([]) == ("", "")


def test_the_script_takes_the_lock_skips_comments_and_records_the_digest() -> None:
    text = script()
    assert "exec 9> /run/apply-ru.lock" in text and "flock -x 9" in text
    assert text.index("flock -x 9") < text.index("ip route show default")  # замок до любых действий
    assert '[[ $cidr == "#"* ]] && continue' in text
    assert text.index("ip rule del priority") < text.index(
        'mv -f "$APPLIED.tmp" "$APPLIED"'
    )  # после успешного переключения
    assert 'rm -f "$APPLIED"' in text  # файл без отпечатка не оставляет старую отметку


async def test_apply_records_the_digest_and_the_status_reports_it() -> None:
    fake = await installed()
    status = await call(fake, "apply_ru", [A, B])
    digest = ru_digest(render_ru_prefixes([A, B]))
    assert status.digest == digest  # type: ignore[attr-defined]
    assert fake.files[ru_applied_file(GATEWAY_LAYOUT)].strip() == digest
    await call(fake, "apply_ru", [A])
    assert (await call(fake, "ru_status")).digest == ru_digest(render_ru_prefixes([A]))  # type: ignore[attr-defined]


async def test_a_failed_apply_keeps_the_previous_digest() -> None:
    fake = await installed()
    await call(fake, "apply_ru", [A, B])
    previous = fake.files[ru_applied_file(GATEWAY_LAYOUT)]
    fake.ru_apply_fails = True
    with pytest.raises(CommandExecutionError):
        await call(fake, "apply_ru", [A])
    assert fake.files[ru_applied_file(GATEWAY_LAYOUT)] == previous


async def test_the_status_has_no_digest_before_the_first_apply() -> None:
    fake = await installed()
    assert (await call(fake, "ru_status")).digest is None  # type: ignore[attr-defined]
