"""AmneziaGatewayProvider (этап 24.1) поверх фейкового сервера: контейнер шлюза, start.sh, правила, порт, health.

Фейк не исполняет bash. Содержимое start.sh проверяется здесь по тексту (и `bash -n`, если bash есть), а действие
скрипта на настоящем контейнере - живым прогоном (см. src/infrastructure/vpn/CLAUDE.md).
"""

import base64
import json
import re
import shutil
import subprocess
import zlib
from ipaddress import IPv4Address
from typing import Any

import pytest

from core.ssh_exception import CommandExecutionError, DockerError, ForeignContainerError
from infrastructure.ssh.client import CommandResult
from infrastructure.vpn import amnezia, gateway
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.awg_config import (
    generate_secret_keys,
    interface_values,
    list_peers,
    render_awg_config,
    render_client_config,
)
from infrastructure.vpn.config import AMNEZIAWG_IMAGE, CLIENT_DNS, CONTAINER_NAME, HOST_CONFIG_DIR, ContainerLayout
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import (
    GATEWAY_LAYOUT,
    GATEWAY_SYSCTLS,
    exit_interfaces,
    has_kill_switch,
    parse_published_ports,
    render_start_script,
)
from infrastructure.vpn.provider import HealthCheck, ProviderState
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient

CONFIG = "/opt/vpn-admin/gw/gw0.conf"
START = "/opt/vpn-admin/gw/start.sh"


@pytest.fixture(autouse=True)
def fixed_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "pick_udp_port", lambda: 31337)


def new_server() -> FakeServer:
    return FakeServer(layout=GATEWAY_LAYOUT)


async def installed() -> FakeServer:
    fake = new_server()
    async with FakeSSHClient(fake) as ssh:
        await AmneziaGatewayProvider(ssh).install()
    return fake


async def call(fake: FakeServer, method: str, *args: object) -> Any:
    async with FakeSSHClient(fake) as ssh:
        return await getattr(AmneziaGatewayProvider(ssh), method)(*args)


def checks(health: Any) -> dict[str, HealthCheck]:
    return {check.name: check for check in health.checks}


# --- раскладка --------------------------------------------------------------------------------------------------


def test_gateway_layout_is_the_agreed_one() -> None:
    layout = GATEWAY_LAYOUT
    assert (layout.container_name, layout.host_config_dir, layout.container_config_dir) == (
        "vpn-admin-gw",
        "/opt/vpn-admin/gw",
        "/etc/vpn-admin/gw",
    )
    assert (layout.config_file_name, layout.interface_name) == ("gw0.conf", "gw0")
    assert (layout.tunnel_subnet, layout.server_address) == ("10.77.0.0/22", "10.77.0.1/22")
    assert layout.client_dns == CLIENT_DNS == "1.1.1.1, 1.0.0.1"  # DNS идёт через туннель и выход
    assert AmneziaGatewayProvider.layout is GATEWAY_LAYOUT
    assert AmneziaWGProvider.layout is not GATEWAY_LAYOUT


def test_provider_factory_maps_the_new_protocol_type() -> None:
    from modules.protocols.models.protocol import ProtocolType
    from modules.servers.service import provider_class

    assert provider_class(ProtocolType.AMNEZIA_GATEWAY) is AmneziaGatewayProvider
    assert provider_class(ProtocolType.AMNEZIA_WG) is AmneziaWGProvider


# --- start.sh ---------------------------------------------------------------------------------------------------


def script() -> str:
    return render_start_script(GATEWAY_LAYOUT)


def test_start_script_is_stable_text() -> None:
    assert render_start_script(GATEWAY_LAYOUT) == script()
    assert script().startswith("#!/bin/bash\n") and script().endswith("\n")
    assert not re.search(r"@[A-Z_]+@", script()), "unreplaced placeholder"


def test_start_script_uses_only_tools_that_exist_in_the_image() -> None:
    """В образе нет nftables, ipset, dnsmasq, conntrack - есть iproute2, iptables, bash, awg, awg-quick."""
    for forbidden in ("nft ", "ipset", "dnsmasq", "conntrack", "curl", "wget", "sysctl", "modprobe"):
        assert forbidden not in script(), forbidden


def test_start_script_installs_the_routing_rules_and_kill_switch() -> None:
    text = script()
    assert "SUBNET=10.77.0.0/22" in text and "GW_IF=gw0" in text and "GW_DIR=/etc/vpn-admin/gw" in text
    assert "ensure_rule 100 'lookup (200|ru)' lookup 200" in text
    assert "ensure_rule 9000 prohibit prohibit" in text
    assert 'ip rule add priority "$prio" from "$SUBNET" "$@"' in text
    # Повторный запуск не плодит правила: перед add - проверка по ip rule show.
    assert 'ip rule show | grep -Eq "^${prio}:' in text


def test_start_script_firewall_rules_are_idempotent_and_not_duplicated() -> None:
    text = script()
    assert 'iptables -t "$table" -C "$@" 2>/dev/null || iptables -t "$table" -A "$@"' in text
    assert 'ensure_ipt filter FORWARD -i "$GW_IF" -o "$GW_IF" -j DROP' in text
    assert "ensure_ipt mangle FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu" in text
    # NAT даёт PostUp серверного конфига - скрипт его не повторяет.
    assert "MASQUERADE" not in text and "-t nat" not in text


def test_start_script_order_kill_switch_first_then_interface_exits_and_alive() -> None:
    text = script()
    positions = [
        text.index("ensure_rule 100"),
        text.index("ensure_rule 9000"),
        text.index("ensure_ipt filter FORWARD"),
        text.index('awg-quick up "$GW_CONF"'),
        text.index('awg-quick up "$conf"'),
        text.index("ensure_ipt mangle"),
        text.index("tail -f /dev/null"),
    ]
    assert positions == sorted(positions)  # kill-switch действует ещё до подъёма gw0


def test_start_script_interface_failure_stops_the_container_but_a_failed_exit_does_not() -> None:
    text = script()
    assert 'awg-quick up "$GW_CONF" || { echo "start.sh: $GW_IF failed to come up" >&2; exit 1; }' in text
    assert 'awg-quick up "$conf" || echo "start.sh: exit tunnel $name failed to come up" >&2' in text
    assert 'if [ -d "$GW_DIR/exits" ]; then' in text and '[ -e "$conf" ] || continue' in text
    assert "set -eu" in text


def test_start_script_apply_mode_skips_interface_and_keepalive() -> None:
    text = script()
    assert 'MODE="${1:-run}"' in text
    assert 'if [ "$MODE" != apply ]; then\n    awg-quick up "$GW_CONF"' in text
    assert 'if [ "$MODE" = apply ]; then\n    exit 0\nfi' in text
    assert text.index('if [ "$MODE" = apply ]') < text.index("tail -f /dev/null")
    assert "trap 'exit 0' TERM INT" in text  # docker stop не ждёт 10 секунд до SIGKILL


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_start_script_is_valid_bash() -> None:
    # Байты, а не текст: в текстовом режиме Windows сломал бы переводы строк, и bash отказал бы.
    result = subprocess.run(["bash", "-n"], input=script().encode(), capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


def test_start_script_follows_the_layout() -> None:
    other = ContainerLayout(
        container_name="x",
        host_config_dir="/h",
        container_config_dir="/c/x",
        config_file_name="x0.conf",
        interface_name="x0",
        tunnel_subnet="10.9.0.0/24",
        client_dns="9.9.9.9",
    )
    text = render_start_script(other)
    assert "SUBNET=10.9.0.0/24" in text and "GW_IF=x0" in text and 'GW_CONF="$GW_DIR/x0.conf"' in text
    assert "10.77" not in text and "gw0" not in text and "vpn-admin" not in text


# --- разбор вывода ---------------------------------------------------------------------------------------------


def test_has_kill_switch_reads_ip_rule_output() -> None:
    subnet = "10.77.0.0/22"
    good = "0:\tfrom all lookup local\n100:\tfrom 10.77.0.0/22 lookup 200\n9000:\tfrom 10.77.0.0/22 prohibit\n"
    assert has_kill_switch(good, subnet)
    assert not has_kill_switch("0:\tfrom all lookup local\n32766:\tfrom all lookup main\n", subnet)
    assert not has_kill_switch("9000:\tfrom 10.8.1.0/24 prohibit\n", subnet)  # чужая подсеть
    assert not has_kill_switch("9001:\tfrom 10.77.0.0/22 prohibit\n", subnet)  # другой приоритет
    assert not has_kill_switch("9000:\tfrom 10.77.0.0/22 lookup main\n", subnet)  # не prohibit
    assert not has_kill_switch("9000:\tfrom 10.77.0.0/220 prohibit\n", subnet)
    assert not has_kill_switch("", subnet)


def test_exit_interfaces_are_all_tunnels_but_the_client_one() -> None:
    assert exit_interfaces("gw0\n", "gw0") == []
    assert exit_interfaces("gw0 ex1 ex2\n", "gw0") == ["ex1", "ex2"]
    assert exit_interfaces("", "gw0") == []


def test_published_ports_parse_single_ranges_and_garbage() -> None:
    output = (
        "0.0.0.0:34567->34567/udp, [::]:34567->34567/udp, 0.0.0.0:80->80/tcp\n"
        "\n"
        "0.0.0.0:20000-20010->20000-20010/udp\n"
        "9000/tcp\n"  # только expose: на хосте ничего не занято
        "0.0.0.0:99999->1/udp, mess->->, 0.0.0.0:5->5/udp, 127.0.0.1:12->12/tcp\n"
    )
    assert parse_published_ports(output) == [(34567, 34567), (34567, 34567), (80, 80), (20000, 20010), (5, 5), (12, 12)]
    assert parse_published_ports("") == []


# --- команды: allowlist ---------------------------------------------------------------------------------------


def _alt(*parts: str) -> str:
    return "(?:" + "|".join(parts) + ")"


E = re.escape
NAME, HOST_DIR, CONT_DIR = "vpn-admin-gw", "/opt/vpn-admin/gw", "/etc/vpn-admin/gw"
SYSCTL_RE = " ".join(f"--sysctl {E(item)}" for item in GATEWAY_SYSCTLS)
ALLOWED = [
    re.compile(pattern)
    for pattern in (
        rf"docker ps -a --filter 'name=\^/\?{NAME}\$' --format '\{{\{{\.{_alt('State', 'Status', 'Image')}\}}\}}'",
        E("docker ps -a --format '{{.Ports}}'"),
        E("docker inspect --type container -f '{{range .Mounts}}{{.Source}} {{end}}' ") + NAME,
        E("docker inspect -f '{{.State.Running}}' ") + NAME,
        E("docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' ") + NAME,
        E("mkdir -p /opt/vpn-admin"),
        E(f"test -d {HOST_DIR}"),
        E(f"rm -f {CONFIG}"),
        E(f"rm -f {START}"),
        E(f"test -f {CONFIG}"),
        E(f"grep 'ListenPort' {CONFIG} | cut -d' ' -f3"),
        (
            rf"docker run -d --name {NAME} --restart unless-stopped --cap-add=NET_ADMIN --device=/dev/net/tun "
            rf"{SYSCTL_RE} -p (\d+):\1/udp -v {E(HOST_DIR)}:{E(CONT_DIR)} {E(AMNEZIAWG_IMAGE)} "
            rf"bash {E(CONT_DIR)}/start\.sh"
        ),
        E(f"docker start {NAME}"),
        E(f"docker stop {NAME} && docker rm {NAME}"),
        E(f"rm -rf {HOST_DIR}"),
        rf"docker port {NAME} \d+/udp",
        rf"docker update --restart unless-stopped {NAME}",
        rf"docker exec {NAME} awg show gw0 (?:public-key|listen-port|latest-handshakes)",
        E(f"docker exec {NAME} awg show interfaces"),
        E(f"docker exec {NAME} ip rule show"),
        E(f"docker exec {NAME} bash {CONT_DIR}/start.sh apply"),
        E(f"docker exec {NAME} iptables -t nat -C POSTROUTING -s 10.77.0.0/22 -o eth0 -j MASQUERADE"),
        E(f"docker exec {NAME} sh -c '{GATEWAY_LAYOUT.nat_post_up}'"),
        E(f"docker exec {NAME} sh -c 'awg-quick strip {CONT_DIR}/gw0.conf > /tmp/gw0.stripped")
        + E(" && awg syncconf gw0 /tmp/gw0.stripped; rc=$?; rm -f /tmp/gw0.stripped; exit $rc'"),
    )
]


def assert_only_allowed(fake: FakeServer) -> None:
    assert fake.commands
    for command in fake.commands:
        collapsed = " ".join(command.split())
        assert any(pattern.fullmatch(collapsed) for pattern in ALLOWED), f"command not in the allowlist: {collapsed!r}"


async def full_flow(fake: FakeServer) -> None:
    async with FakeSSHClient(fake) as ssh:
        provider = AmneziaGatewayProvider(ssh)
        await provider.install()
        client = await provider.create_client("alice")
        await provider.sync()
        await provider.health_check()
        await provider.install()
        await provider.revoke_client(client.public_key)
        await provider.uninstall()


async def test_every_ssh_command_of_the_gateway_is_on_the_allowlist() -> None:
    fake = new_server()
    await full_flow(fake)
    assert_only_allowed(fake)
    for command in fake.commands:
        for marker in ("amnezia-awg", "/opt/vpn-admin/awg", "awg0", "/etc/amnezia/awg", "10.8.1."):
            assert marker not in command, f"{marker!r} in {command!r}"


async def test_gateway_never_runs_commands_built_from_outside_input() -> None:
    """Имя клиента не попадает в команды шлюза: оно только в комментарий конфига, который пишется через SFTP."""
    fake = await installed()
    before = len(fake.commands)
    await call(fake, "create_client", "bob")
    assert all("bob" not in command for command in fake.commands[before:])
    assert "# client: bob" in fake.files[CONFIG]


# --- install: docker run, порядок записи, права ----------------------------------------------------------------


async def test_install_runs_the_container_with_sysctls_and_the_start_script() -> None:
    fake = await installed()
    (run,) = [" ".join(c.split()) for c in fake.commands if c.startswith("docker run")]
    assert " --sysctl net.ipv4.ip_forward=1" in run
    assert " --sysctl net.ipv4.conf.all.rp_filter=2" in run
    assert " --sysctl net.ipv4.conf.default.rp_filter=2" in run
    assert " --sysctl net.ipv6.conf.all.disable_ipv6=1" in run
    assert fake.container_sysctls[NAME] == list(GATEWAY_SYSCTLS)
    assert run.endswith(f"{AMNEZIAWG_IMAGE} bash /etc/vpn-admin/gw/start.sh")
    assert "--cap-add=NET_ADMIN --device=/dev/net/tun" in run and "--restart unless-stopped" in run
    assert "-p 31337:31337/udp -v /opt/vpn-admin/gw:/etc/vpn-admin/gw " in run
    assert "sh -c" not in run and "awg-quick up" not in run  # запуск - только скриптом
    assert fake.restart_policy == "unless-stopped"


async def test_install_writes_config_then_start_script_before_docker_run_with_closed_permissions() -> None:
    fake = await installed()
    assert fake.writes == [CONFIG, START]  # конфиг, скрипт; docker run - после (иначе контейнер вышел бы)
    assert fake.running and fake.start_script_runs == ["run"]
    assert fake.files[START] == render_start_script(GATEWAY_LAYOUT)
    assert fake.file_modes[START] == fake.file_modes[CONFIG] == 0o600
    assert fake.dirs[HOST_DIR] == 0o700  # папка с приватным ключом сервера закрыта
    assert fake.commands.index("mkdir -p /opt/vpn-admin") < next(
        i for i, c in enumerate(fake.commands) if c.startswith("docker run")
    )


async def test_installed_gateway_state_is_running_with_rules_and_server_config() -> None:
    fake = await installed()
    assert fake.ip_rules == [
        "100:\tfrom 10.77.0.0/22 lookup 200",
        "9000:\tfrom 10.77.0.0/22 prohibit",
    ]
    assert fake.firewall == {"isolation", "mss"} and fake.nat_rule
    values = interface_values(fake.files[CONFIG])
    assert values["Address"] == "10.77.0.1/22" and values["ListenPort"] == "31337"
    assert values["PostUp"] == GATEWAY_LAYOUT.nat_post_up  # NAT для российского напрямую - из конфига
    status = await call(fake, "get_status")
    assert status.server_status == ProviderState.RUNNING and status.port == 31337


async def test_install_is_idempotent_and_keeps_keys_port_and_clients() -> None:
    fake = await installed()
    client = await call(fake, "create_client", "alice")
    config_before = fake.files[CONFIG]
    runs_before = sum(c.startswith("docker run") for c in fake.commands)

    status = await call(fake, "install")
    status = await call(fake, "install")

    assert fake.files[CONFIG] == config_before  # ключи сервера, порт и клиенты не тронуты
    assert sum(c.startswith("docker run") for c in fake.commands) == runs_before  # второй контейнер не создаётся
    assert status.server_status == ProviderState.RUNNING and status.port == 31337
    assert fake.start_script_runs == ["run", "apply", "apply"]  # правила дочиниваются без перезапуска
    assert not any(c.startswith(("docker stop", "docker restart", "docker rm")) for c in fake.commands)
    assert client.public_key in fake.config_peer_keys()
    assert_only_allowed(fake)


async def test_reinstall_rewrites_an_outdated_start_script_and_applies_it() -> None:
    fake = await installed()
    fake.files[START] = "#!/bin/bash\n# старая версия панели\n"
    writes_before = len(fake.writes)
    await call(fake, "install")
    assert fake.files[START] == render_start_script(GATEWAY_LAYOUT)
    assert fake.writes[writes_before:] == [START]  # конфиг не переписывается
    assert fake.start_script_runs[-1] == "apply"


async def test_reinstall_does_not_rewrite_an_up_to_date_start_script() -> None:
    fake = await installed()
    writes_before = len(fake.writes)
    await call(fake, "install")
    assert fake.writes[writes_before:] == []


async def test_reinstall_recreates_a_missing_container_with_the_same_port() -> None:
    fake = await installed()
    fake.containers.pop(NAME)
    fake.mounts.pop(NAME)
    fake.ip_rules, fake.firewall = [], set()
    status = await call(fake, "install")
    assert status.server_status == ProviderState.RUNNING and status.port == 31337
    assert fake.ip_rules and fake.start_script_runs == ["run", "run"]


async def test_reinstall_starts_a_stopped_container() -> None:
    fake = await installed()
    fake.containers[NAME] = "exited"
    status = await call(fake, "install")
    assert status.server_status == ProviderState.RUNNING
    assert f"docker start {NAME}" in fake.commands


async def test_reinstall_fixes_restart_policy_and_nat_hooks_like_the_base_provider() -> None:
    fake = await installed()
    fake.restart_policy = "no"
    fake.files[CONFIG] = "\n".join(line for line in fake.files[CONFIG].splitlines() if "Post" not in line)
    fake.nat_rule = False
    await call(fake, "install")
    assert fake.restart_policy == "unless-stopped"
    assert GATEWAY_LAYOUT.nat_post_up in fake.files[CONFIG] and fake.nat_rule


async def test_failed_apply_on_reinstall_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = await installed()
    original = fake.run

    def run(command: str) -> CommandResult:
        if command.endswith("start.sh apply"):
            return CommandResult("", "boom", 1)
        return original(command)

    monkeypatch.setattr(fake, "run", run)
    with pytest.raises(CommandExecutionError, match="Failed to apply gateway rules"):
        await call(fake, "install")


# --- откат первой установки ------------------------------------------------------------------------------------


async def test_failed_first_install_leaves_no_container_and_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Контейнер вышел (скрипт не тот): остаётся ни контейнера, ни папки - повторная установка выберет новый порт."""
    monkeypatch.setattr(gateway, "render_start_script", lambda layout: "#!/bin/bash\nexit 1\n")
    fake = new_server()
    with pytest.raises(DockerError, match="Failed to start container"):
        await call(fake, "install")
    assert fake.containers == {} and fake.files == {}
    assert f"rm -rf {HOST_DIR}" in fake.commands

    monkeypatch.undo()
    await call(fake, "install")  # после отката установка проходит заново
    assert fake.running


async def test_start_script_write_failure_aborts_before_docker_run_and_cleans_up() -> None:
    fake = new_server()
    fake.write_fails.add(START)
    with pytest.raises(Exception, match="File write"):
        await call(fake, "install")
    assert not any(c.startswith("docker run") for c in fake.commands)
    assert fake.files == {} and fake.containers == {}


async def test_docker_run_port_conflict_with_a_host_process_rolls_the_install_back() -> None:
    """Порт занят процессом вне Docker (`docker ps` его не показывает): docker run отказывает, следов не остаётся."""
    fake = new_server()
    fake.host_busy_ports.add(31337)
    with pytest.raises(DockerError, match="already allocated"):
        await call(fake, "install")
    assert fake.files == {} and fake.containers == {}
    assert f"rm -rf {HOST_DIR}" in fake.commands


async def test_existing_container_without_config_is_an_error_not_a_first_install() -> None:
    """Свой контейнер работает, а gw0.conf пропал: новый конфиг получил бы другой порт, `docker run` не сделали бы
    (контейнер уже есть), в БД попал бы порт, на котором шлюз не слушает. Ошибка, и ничего не меняется."""
    fake = await installed()
    del fake.files[CONFIG]
    before_files, before_port = dict(fake.files), fake.container_ports[NAME]
    commands_before = len(fake.commands)
    with pytest.raises(CommandExecutionError, match="exists but .*gw0.conf is missing"):
        await call(fake, "install")
    assert fake.files == before_files and fake.writes == [CONFIG, START]  # новых записей нет
    assert fake.containers == {NAME: "running"} and fake.container_ports[NAME] == before_port
    new = fake.commands[commands_before:]
    assert not any(c.startswith(("docker run", "docker stop", "docker rm", "rm ", "mkdir")) for c in new)
    assert fake.ip_rules  # правила работающего контейнера на месте


@pytest.mark.parametrize("state", ["running", "exited"])
async def test_existing_container_without_config_never_gets_rolled_back(state: str) -> None:
    fake = new_server()
    fake.containers[NAME] = state
    fake.mounts[NAME] = [HOST_DIR]
    with pytest.raises(CommandExecutionError, match="exists but"):
        await call(fake, "install")
    assert fake.containers == {NAME: state} and fake.writes == [] and fake.dirs == {}
    assert fake.mutating_commands() == []


async def test_rollback_removes_only_what_this_call_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """Папка шлюза уже была (и в ней чужой файл): откат убирает свои gw0.conf и start.sh, а не всю папку."""
    monkeypatch.setattr(gateway, "render_start_script", lambda layout: "#!/bin/bash" + chr(10) + "exit 1" + chr(10))
    fake = new_server()
    fake.dirs[HOST_DIR] = 0o700
    fake.files[f"{HOST_DIR}/notes.txt"] = "keep me"
    with pytest.raises(DockerError, match="Failed to start container"):
        await call(fake, "install")
    assert fake.files == {f"{HOST_DIR}/notes.txt": "keep me"} and NAME not in fake.containers
    assert f"rm -rf {HOST_DIR}" not in fake.commands
    assert f"rm -f {CONFIG}" in fake.commands and f"rm -f {START}" in fake.commands


async def test_rollback_does_not_touch_a_container_the_call_did_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбой записи start.sh до docker run: контейнер не создавался - `docker stop/rm` не вызываются."""
    fake = new_server()
    fake.write_fails.add(START)
    with pytest.raises(Exception, match="File write"):
        await call(fake, "install")
    assert not any(c.startswith(("docker stop", "docker rm", "docker run")) for c in fake.commands)


# --- чужой контейнер (защита 23.1) ------------------------------------------------------------------------------


def client_config_text() -> str:
    keys = generate_secret_keys()
    server_config = render_awg_config(
        generate_secret_keys().private_key, 31337, tunnel_subnet="10.77.0.0/22", address="10.77.0.1/22"
    )
    return render_client_config(
        server_config=server_config,
        client_private_key=keys.private_key,
        client_address=IPv4Address("10.77.0.2"),
        psk=keys.psk,
        server_public_key=generate_secret_keys().public_key,
        endpoint_host="203.0.113.10",
    )


@pytest.fixture(params=["volume", "default_panel_folder"])
def foreign(request: pytest.FixtureRequest) -> FakeServer:
    fake = new_server()
    source = "/var/lib/docker/volumes/vpn-admin-gw/_data" if request.param == "volume" else HOST_CONFIG_DIR
    fake.add_foreign_container(source=source)
    return fake


@pytest.mark.parametrize("method", ["install", "create_client", "revoke_client", "restore_client"])
async def test_foreign_container_named_like_the_gateway_is_refused(foreign: FakeServer, method: str) -> None:
    args = {
        "create_client": ("bob",),
        "revoke_client": ("A" * 43 + "=",),
        "restore_client": ("bob", client_config_text()),
    }.get(method, ())
    async with FakeSSHClient(foreign) as ssh:
        with pytest.raises(ForeignContainerError, match="vpn-admin-gw"):
            await getattr(AmneziaGatewayProvider(ssh), method)(*args)
    assert foreign.mutating_commands() == [] and foreign.writes == [] and foreign.dirs == {}
    assert foreign.containers == {NAME: "running"}


async def test_foreign_container_is_not_stopped_removed_or_reported(foreign: FakeServer) -> None:
    assert (await call(foreign, "uninstall")).server_status == ProviderState.NOT_INSTALLED
    assert (await call(foreign, "get_status")).server_status == ProviderState.NOT_INSTALLED
    assert await call(foreign, "list_clients") == []
    health = await call(foreign, "health_check")
    assert not health.installed and set(checks(health)) == {"container"}  # шлюзовых проверок нет
    assert foreign.mutating_commands() == [] and foreign.containers == {NAME: "running"}
    assert not any("docker exec" in c for c in foreign.commands)


async def test_regular_container_on_the_same_server_is_neither_touched_nor_touching() -> None:
    fake = new_server()
    fake.containers[CONTAINER_NAME] = "running"  # обычный протокол панели рядом
    fake.mounts[CONTAINER_NAME] = [HOST_CONFIG_DIR]
    fake.files[f"{HOST_CONFIG_DIR}/awg0.conf"] = "regular config"
    fake.container_ports[CONTAINER_NAME] = [40000]
    await full_flow(fake)
    assert fake.containers == {CONTAINER_NAME: "running"}
    assert fake.files == {f"{HOST_CONFIG_DIR}/awg0.conf": "regular config"}
    assert not any(CONTAINER_NAME in c or f"{HOST_CONFIG_DIR}/" in c for c in fake.commands)
    assert_only_allowed(fake)


async def test_regular_provider_ignores_a_running_gateway() -> None:
    fake = await installed()
    async with FakeSSHClient(fake) as ssh:
        regular = AmneziaWGProvider(ssh)
        assert (await regular.get_status()).server_status == ProviderState.NOT_INSTALLED
        assert await regular.list_clients() == []
    assert fake.containers == {NAME: "running"} and START in fake.files


async def test_uninstall_removes_only_the_gateway() -> None:
    fake = new_server()
    fake.files["/opt/vpn-admin/awg/awg0.conf"] = "regular"
    fake.files["/opt/vpn-admin/stage/x"] = "stage"
    async with FakeSSHClient(fake) as ssh:
        provider = AmneziaGatewayProvider(ssh)
        await provider.install()
        status = await provider.uninstall()
    assert status.server_status == ProviderState.NOT_INSTALLED
    assert fake.containers == {} and fake.ip_rules == [] and fake.firewall == set()
    assert fake.files == {"/opt/vpn-admin/awg/awg0.conf": "regular", "/opt/vpn-admin/stage/x": "stage"}


# --- порт -------------------------------------------------------------------------------------------------------


async def test_port_never_matches_a_port_published_by_another_container(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = iter([40000, 40000, 40001, 50000])
    monkeypatch.setattr(gateway, "pick_udp_port", lambda: next(candidates))
    fake = new_server()
    fake.containers[CONTAINER_NAME] = "running"
    fake.mounts[CONTAINER_NAME] = [HOST_CONFIG_DIR]
    fake.container_ports[CONTAINER_NAME] = [40000]
    fake.containers["web"] = "running"
    fake.container_ports["web"] = [40001]
    status = await call(fake, "install")
    assert status.port == 50000 and interface_values(fake.files[CONFIG])["ListenPort"] == "50000"
    assert "docker ps -a --format '{{.Ports}}'" in fake.commands


async def test_port_range_published_by_another_container_is_avoided(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = iter([20005, 21000])
    monkeypatch.setattr(gateway, "pick_udp_port", lambda: next(candidates))
    fake = new_server()
    fake.containers["range"] = "running"
    fake.container_ports["range"] = []
    monkeypatch.setattr(
        fake, "_published", lambda name: "0.0.0.0:20000-20010->20000-20010/udp" if name == "range" else ""
    )
    assert (await call(fake, "install")).port == 21000


async def test_port_pick_gives_up_when_every_attempt_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = new_server()
    fake.containers["web"] = "running"
    fake.container_ports["web"] = [31337]  # pick_udp_port всегда возвращает 31337
    with pytest.raises(CommandExecutionError, match="free UDP port"):
        await call(fake, "install")
    assert fake.writes == [] and fake.containers == {"web": "running"}


async def test_unreadable_docker_ps_is_an_error_not_a_free_port() -> None:
    fake = new_server()
    fake.ps_fails = True
    with pytest.raises(CommandExecutionError):
        await call(fake, "install")
    assert fake.mutating_commands() == []


async def test_base_provider_picks_its_port_without_looking_at_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обычный протокол не менялся: пул портов он не читает (golden-журнал команд остаётся прежним)."""
    monkeypatch.setattr(amnezia, "pick_udp_port", lambda: 31337)
    fake = FakeServer()
    async with FakeSSHClient(fake) as ssh:
        await AmneziaWGProvider(ssh).install()
    assert "docker ps -a --format '{{.Ports}}'" not in fake.commands


# --- клиенты ----------------------------------------------------------------------------------------------------


def decode(link: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(link.removeprefix("vpn://") + "=" * (-len(link.removeprefix("vpn://")) % 4))
    document: dict[str, Any] = json.loads(zlib.decompress(raw[4:]))
    return document


async def test_create_client_gets_an_address_from_the_gateway_subnet() -> None:
    fake = await installed()
    async with FakeSSHClient(fake) as ssh:
        provider = AmneziaGatewayProvider(ssh)
        first = await provider.create_client("grant-aaaa")
        second = await provider.create_client("grant-bbbb")
    assert (first.address, second.address) == ("10.77.0.2", "10.77.0.3")
    assert [peer.name for peer in list_peers(fake.files[CONFIG])] == ["grant-aaaa", "grant-bbbb"]
    assert "Address = 10.77.0.2/32" in first.config and f"DNS = {CLIENT_DNS}\n" in first.config
    assert "Endpoint = 203.0.113.10:31337" in first.config
    assert fake.config_peer_keys() == fake.interface_peers  # peer применён к живому интерфейсу


async def test_share_link_carries_the_gateway_subnet_and_dns() -> None:
    fake = await installed()
    client = await call(fake, "create_client", "alice")
    document = decode(AmneziaGatewayProvider.share_link(client.config, "RU gateway") or "")
    assert document["containers"][0]["awg"]["subnet_address"] == "10.77.0.0"
    assert (document["dns1"], document["dns2"]) == ("1.1.1.1", "1.0.0.1")
    assert document["description"] == "RU gateway"
    assert AmneziaGatewayProvider.share_qr_codes(client.config, "RU gateway")


async def test_revoke_and_restore_work_on_the_gateway_config() -> None:
    fake = await installed()
    client = await call(fake, "create_client", "alice")
    await call(fake, "revoke_client", client.public_key)
    assert client.public_key not in fake.config_peer_keys()
    assert await call(fake, "restore_client", "alice", client.config) is True
    assert client.public_key in fake.config_peer_keys() and client.public_key in fake.interface_peers


# --- health -----------------------------------------------------------------------------------------------------


async def test_health_of_a_fresh_gateway_fails_only_because_there_are_no_exits() -> None:
    fake = await installed()
    health = await call(fake, "health_check")
    by_name = checks(health)
    assert [name for name, check in by_name.items() if not check.ok] == ["exits"]
    assert by_name["exits"].detail == "no exit tunnels"
    assert by_name["kill_switch"].ok and by_name["nat"].ok and by_name["restart_policy"].ok
    assert health.installed and health.port == 31337


async def test_health_passes_when_an_exit_tunnel_is_up() -> None:
    fake = await installed()
    fake.exit_interfaces = ["ex1"]
    health = await call(fake, "health_check")
    assert all(check.ok for check in health.checks), health.checks
    assert checks(health)["exits"].detail == "1 exit tunnel(s)"


async def test_health_detects_a_missing_kill_switch() -> None:
    fake = await installed()
    fake.exit_interfaces = ["ex1"]
    fake.ip_rules = [rule for rule in fake.ip_rules if "prohibit" not in rule]
    health = await call(fake, "health_check")
    assert [name for name, check in checks(health).items() if not check.ok] == ["kill_switch"]


async def test_health_ignores_a_prohibit_rule_of_a_different_subnet() -> None:
    fake = await installed()
    fake.exit_interfaces = ["ex1"]
    fake.ip_rules = ["9000:\tfrom 10.8.1.0/24 prohibit"]
    assert not checks(await call(fake, "health_check"))["kill_switch"].ok


async def test_health_of_a_stopped_gateway_has_no_in_container_checks() -> None:
    fake = await installed()
    fake.containers[NAME] = "exited"
    health = await call(fake, "health_check")
    assert not checks(health)["container"].ok and "kill_switch" not in checks(health) and "exits" not in checks(health)
    assert not any("docker exec" in c for c in fake.commands[-6:])


async def test_health_of_a_server_without_gateway_is_quiet() -> None:
    fake = new_server()
    health = await call(fake, "health_check")
    assert not health.installed and set(checks(health)) == {"container", "config"}
    assert fake.mutating_commands() == []


async def test_sync_snapshot_of_a_server_without_gateway_is_not_installed() -> None:
    fake = new_server()
    snapshot = await call(fake, "sync")
    assert snapshot.provider_status.server_status == ProviderState.NOT_INSTALLED and snapshot.client_list == []
    assert fake.mutating_commands() == []
