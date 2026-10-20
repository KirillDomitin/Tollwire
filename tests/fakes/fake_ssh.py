"""Фейковый Linux-сервер с AmneziaWG и SSH-клиент к нему.

FakeSSHClient повторяет интерфейс infrastructure.ssh.client.SSHClient (connect/close/execute/read_file/write_file),
а FakeServer держит состояние «сервера» и отвечает на ровно те команды, которые шлёт наш код. Незнакомая команда —
AssertionError: если провайдер начнёт слать что-то новое, тест сразу это покажет, а не тихо получит пустой ответ.
"""

import fnmatch
import ipaddress
import json
import posixpath
import re
import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import asyncssh

from core.exceptions import SSHAuthError, SSHConnectionError
from infrastructure.monitoring.host_metrics import HOST_METRICS_COMMAND
from infrastructure.ssh.client import CommandResult
from infrastructure.vpn.awg_config import (
    client_config_sections,
    generate_secret_keys,
    interface_values,
    is_valid_public_key,
    list_peers,
    public_key_from_private,
)
from infrastructure.vpn.config import (
    CONFIG_FILE_NAME,
    CONTAINER_NAME,
    DEFAULT_LAYOUT,
    HOST_CONFIG_DIR,
    INTERNET_PROBE_COMMAND,
    ContainerLayout,
)
from infrastructure.vpn.gateway_config import (
    AWG_SHOW_INTERFACES,
    DOCKER_PUBLISHED_PORTS,
    EXIT_RULE_PRIORITY,
    IP_RULE_SHOW,
    KILL_SWITCH_PRIORITY,
    RU_RULE_PRIORITY,
    RU_TABLE,
    assign_apply_command,
    assign_host_file,
    assign_show_command,
    exit_container_file,
    exit_host_dir,
    exit_host_file,
    exit_interface,
    exit_mark,
    exit_rule_down,
    exit_rule_up,
    exit_slot_from_interface,
    exit_sync_command,
    exit_table,
    render_start_script,
    sysctls_command,
)

CONFIG_FILE = f"{HOST_CONFIG_DIR}/{CONFIG_FILE_NAME}"
_PS_RE = re.compile(r"docker ps -a --filter (\S+) --format '([^']*)'")
_PS_FIELD_RE = re.compile(r"\{\{\.(\w+)\}\}")
_INSPECT_RUNNING_RE = re.compile(r"docker inspect (?:--type container )?-f '\{\{\.State\.Running\}\}' (\S+)")
_INSPECT_MOUNTS_RE = re.compile(
    r"docker inspect --type container -f '\{\{range \.Mounts\}\}\{\{\.Source\}\} \{\{end\}\}' (\S+)"
)

# Приложение AmneziaVPN: контейнеры amnezia-awg (AWG v1, утилита wg) и amnezia-awg2 (AWG v2, утилита awg). Конфиг и
# таблица клиентов лежат ВНУТРИ контейнера, на хосте ничего нет.
APP_CONFIG_DIR = "/opt/amnezia/awg"
APP_CLIENTS_TABLE = f"{APP_CONFIG_DIR}/clientsTable"
APP_PRIVATE_KEY_FILE = f"{APP_CONFIG_DIR}/wireguard_server_private_key.key"  # секреты, которые читать нельзя
APP_PSK_FILE = f"{APP_CONFIG_DIR}/wireguard_psk.key"
APP_LAYOUT = {
    "amnezia-awg": {"tool": "wg", "interface": "wg0", "image": "amneziavpn/amnezia-awg:latest"},
    "amnezia-awg2": {"tool": "awg", "interface": "awg0", "image": "amneziavpn/amnezia-awg2:latest"},
}
# docker exec, которые только читают: файл приложения и рукопожатия. Любой иной docker exec — изменяющий.
_READ_ONLY_EXEC = re.compile(
    rf"docker exec \S+ (?:cat {re.escape(APP_CONFIG_DIR)}/[\w.-]+|(?:wg|awg) show \w+ latest-handshakes)"
)


# Запись в контейнер приложения (этап 23.3): SFTP-файл на хосте → `docker cp` → `rm -f`. Промежуточные файлы бывают с
# приватным ключом сервера, поэтому фейк следит, что после любой операции в STAGE_DIR пусто.
STAGE_DIR = "/opt/vpn-admin/stage"
_STAGE_FILE = re.compile(rf"{re.escape(STAGE_DIR)}/[\w.*-]+")  # `*` - очистка остатков: `<контейнер>.*`
_STAGE_PARENT = posixpath.dirname(STAGE_DIR)
_APP_PATHS = rf"{re.escape(APP_CONFIG_DIR)}/(?:wg0\.conf|awg0\.conf|clientsTable)"
# Применение конфига: strip проверяется до syncconf (сломанный strip не должен давать пустой конфиг и сносить peer'ов).
# Группы: утилита strip, путь конфига, утилита syncconf, интерфейс.
_APPLY_SCRIPT = (
    r'c=\$\(((?:wg|awg))-quick strip {strip}\) && \[ -n "\$c" \] && '
    r'((?:wg|awg)) syncconf (\w+) <\(printf "%s\\n" "\$c"\)'
)
# Команды протокола записи (единственные изменяющие, которые допустимы для приложения). Строгие шаблоны: любая
# другая изменяющая команда — «лишняя» (unexpected_changing_commands).
_APP_WRITE_COMMANDS = re.compile(
    rf"docker cp {_STAGE_FILE.pattern} amnezia-awg2?:{_APP_PATHS}"
    rf"|rm -f {_STAGE_FILE.pattern}"
    rf"|mkdir -p {re.escape(_STAGE_PARENT)}"
    rf"|docker exec amnezia-awg2? bash -c '{_APPLY_SCRIPT.format(strip=_APP_PATHS)}'"
)


def apply_command(container: str, tool: str, interface: str, config_path: str) -> str:
    """Команда применения конфига так, как её шлёт провайдер (см. AmneziaAppProvider._apply)."""
    script = (
        f'c=$({tool}-quick strip {config_path}) && [ -n "$c" ] && {tool} syncconf {interface} <(printf "%s\\n" "$c")'
    )
    return f"docker exec {container} bash -c '{script}'"


def stage_conf(container: str) -> str:
    """Промежуточный файл конфига на хосте (в нём бывает приватный ключ сервера)."""
    return f"{STAGE_DIR}/{container}.conf"


def stage_table(container: str) -> str:
    return f"{STAGE_DIR}/{container}.clientsTable"


_ASSIGN_LINE = re.compile(r"-A GW_ASSIGN -s (\d+\.\d+\.\d+\.\d+)/32 -j MARK --set-mark (\d+)")


def _parse_assign_rules(text: str) -> dict[str, int] | None:
    """assign.rules по формату панели: `*mangle`, `:GW_ASSIGN - [0:0]`, правила MARK, `COMMIT`. Иное - None (ошибка)."""
    lines = text.splitlines()
    if lines[:2] != ["*mangle", ":GW_ASSIGN - [0:0]"] or lines[-1:] != ["COMMIT"]:
        return None
    parsed: dict[str, int] = {}
    for line in lines[2:-1]:
        match = _ASSIGN_LINE.fullmatch(line)
        if match is None:
            return None
        ipaddress.IPv4Address(match.group(1))
        parsed[match.group(1)] = int(match.group(2))
    return parsed


def _parse_app_config(text: str, previous: list["AppPeer"]) -> list["AppPeer"]:
    """Как `<tool>-quick strip` + `<tool> syncconf`: разбирает конфиг и возвращает живых peer'ов. Мусор → ValueError.
    Рукопожатия у peer'ов, которые уже были, сохраняются."""
    peers: list[dict[str, str]] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if line not in ("[Interface]", "[Peer]"):
                raise ValueError("unknown section")
            section = line
            if section == "[Peer]":
                peers.append({})
            continue
        if not section or "=" not in line:
            raise ValueError("line unrecognized")
        key, _, value = line.partition("=")
        if section == "[Peer]":
            peers[-1][key.strip()] = value.strip()
    known = {peer.public_key: peer for peer in previous}
    result: list[AppPeer] = []
    for values in peers:
        public_key = values.get("PublicKey", "")
        if not is_valid_public_key(public_key) or any(p.public_key == public_key for p in result):
            raise ValueError("bad or duplicate peer")
        psk = values.get("PresharedKey", "")
        if psk and not is_valid_public_key(psk):
            raise ValueError("bad preshared key")
        allowed = values.get("AllowedIPs", "")
        for item in filter(None, (part.strip() for part in allowed.split(","))):
            ipaddress.ip_network(item, strict=False)
        result.append(AppPeer(public_key, allowed, psk, known[public_key].handshake if public_key in known else 0))
    return result


FOREIGN_MOUNT_SOURCE = "/var/lib/docker/volumes/amnezia-awg/_data"  # том контейнера, созданного AmneziaVPN
INSPECT_MOUNTS = (
    f"docker inspect --type container -f '{{{{range .Mounts}}}}{{{{.Source}}}} {{{{end}}}}' {CONTAINER_NAME}"
)
# Белый список команд, которые ничего не меняют на сервере; всё остальное (включая любой docker exec) — изменяющее.
_READ_ONLY_PREFIXES = (
    "docker ps",
    "docker inspect",
    "docker port",
    "docker --version",
    "test -f",
    "test -d",
    "grep ",
    "uname",
    "id -u",
    "command -v",
    "systemctl is-active",
)

HOME_DIR = "/root"
AUTHORIZED_KEYS = f"{HOME_DIR}/.ssh/authorized_keys"


def key_blob(private_key: str) -> str:
    """Base64-часть публичного ключа (2-е поле строки authorized_keys) для приватного ключа в OpenSSH/PEM."""
    public = asyncssh.import_private_key(private_key.strip()).convert_to_public()
    return public.export_public_key("openssh").decode().split()[1]


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", exit_status=0)


def _fail(stderr: str = "error", code: int = 1) -> CommandResult:
    return CommandResult(stdout="", stderr=stderr, exit_status=code)


@dataclass
class AppPeer:
    """[Peer] конфига приложения AmneziaVPN. Комментариев `# client:` у приложения нет — имена в clientsTable."""

    public_key: str
    allowed_ips: str = "10.8.1.2/32"
    psk: str = field(default_factory=lambda: generate_secret_keys().psk)
    handshake: int = 0  # unix-время последнего рукопожатия, 0 — не было


@dataclass
class AmneziaApp:
    """Контейнер приложения AmneziaVPN. `files` — файловая система ВНУТРИ контейнера (путь → текст)."""

    container: str
    image: str
    tool: str  # wg / awg: другой утилиты в контейнере нет
    interface: str
    config_path: str
    private_key: str
    port: int
    peers: list[AppPeer]
    files: dict[str, str] = field(default_factory=dict)
    interface_up: bool = True  # False — как контейнер, где интерфейс не поднялся: `<tool> show <iface>` падает
    # Запись (23.3). `peers` — «живые» peer'ы интерфейса: после успешного syncconf они берутся из конфига.
    apply_fails: bool = False  # syncconf падает (код 1, stderr без содержимого конфига), интерфейс не меняется
    docker_cp_fails: set[str] = field(default_factory=set)  # пути ВНУТРИ контейнера, `docker cp` в которые падает
    applies: int = 0  # сколько раз syncconf применился успешно
    read_counts: dict[str, int] = field(default_factory=dict)  # путь → сколько раз прочитан (`cat`)
    # Вызывается при каждом `cat` ПЕРЕД ответом: (путь, номер чтения этого пути с 1). Тест подменяет здесь файл —
    # «приложение AmneziaVPN поменяло конфиг между чтением и записью».
    on_read: Callable[[str, int], None] | None = None
    on_apply: Callable[[], None] | None = None  # перед каждым syncconf (например, чтобы сломать откат)

    @property
    def secrets(self) -> list[str]:
        """Всё, что не должно попасть ни в результаты провайдера, ни в тексты ошибок, ни в логи."""
        return [self.private_key, *(peer.psk for peer in self.peers)]

    def render_config(self) -> str:
        lines = [
            "[Interface]",
            f"PrivateKey = {self.private_key}",
            "Address = 10.8.1.0/24",
            f"ListenPort = {self.port}",
            "Jc = 5",
            "Jmin = 10",
            "Jmax = 50",
            "S1 = 60",
            "S2 = 77",
            "H1 = 1020325451",
            "H2 = 3288052141",
            "H3 = 1766607858",
            "H4 = 2528465083",
        ]
        for peer in self.peers:
            lines += ["", "[Peer]", f"PublicKey = {peer.public_key}", f"PresharedKey = {peer.psk}"]
            if peer.allowed_ips:
                lines.append(f"AllowedIPs = {peer.allowed_ips}")
        return "\n".join(lines) + "\n"

    def handshakes_output(self) -> str:
        return "".join(f"{peer.public_key}\t{peer.handshake}\n" for peer in self.peers)


def clients_table_json(names: dict[str, str], *, legacy: bool = False) -> str:
    """clientsTable приложения: публичный ключ → имя. legacy=True — старый формат (объект, ключ — публичный ключ)."""
    if legacy:
        return json.dumps({key: {"clientName": name} for key, name in names.items()})
    return json.dumps(
        [
            {"clientId": key, "userData": {"clientName": name, "creationDate": "Mon Jan 1 12:00:00 2024"}}
            for key, name in names.items()
        ]
    )


@dataclass
class CopyRecord:
    """Успешный `docker cp <файл хоста> <контейнер>:<путь>`: что и с какими правами лежало на хосте."""

    src: str
    container: str
    dst: str
    mode: int | None
    content: str


@dataclass
class FakeServer:
    host: str = "203.0.113.10"
    files: dict[str, str] = field(default_factory=dict)
    containers: dict[str, str] = field(default_factory=dict)  # имя → running / exited
    docker_installed: bool = True
    ps_fails: bool = False  # `docker ps` падает (демон перезапускается): решение «чей контейнер» принять нельзя
    docker_active: bool = True
    internet: bool = True
    reachable: bool = True
    nat_rule: bool = False
    port_published: bool = True
    restart_policy: str = "no"  # RestartPolicy.Name контейнера: no / unless-stopped / always / on-failure
    interface_peers: set[str] = field(default_factory=set)  # ключи, применённые к живому интерфейсу
    handshakes: dict[str, int] = field(default_factory=dict)  # ключ → unix-время последнего рукопожатия
    commands: list[str] = field(default_factory=list)
    # Раскладка контейнера панели на этом «сервере»: по умолчанию как у обычного протокола; шлюз и другие подклассы
    # провайдера с иной раскладкой — свой FakeServer(layout=...). Все ответы фейка собираются только из неё.
    layout: ContainerLayout = DEFAULT_LAYOUT
    # Журнал файловых операций клиента (SFTP): пути, которые прочитали / записали. Тесты «не трогаем чужое».
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    # Источники монтирований контейнера (`docker inspect .Mounts`). Нет записи — контейнер смонтирован как наш
    # (HOST_CONFIG_DIR): так ведут себя тесты, которые выставляют `containers[...]` напрямую.
    mounts: dict[str, list[str]] = field(default_factory=dict)
    dirs: dict[str, int] = field(default_factory=dict)  # каталоги, созданные через SFTP → права (0o700 для .ssh)
    key_auth_enabled: bool = True  # False — как sshd с PubkeyAuthentication no: вход по ключу отвергается
    # Вызывается перед каждой командой, выполняемой по ключу: (клиент, команда). Тест подставляет сюда
    # событие «посреди задачи» (например, смену учётки), не полагаясь на время.
    on_key_command: Callable[[str], Awaitable[None]] | None = None
    # Счётчики /proc для мониторинга: тест сдвигает их между замерами и проверяет расчёт скоростей.
    host_stats: dict[str, float] = field(
        default_factory=lambda: {
            "uptime": 1000.0,
            "cpu_busy": 2000,
            "cpu_idle": 8000,
            "rx": 1_000_000,
            "tx": 500_000,
            "read_sectors": 0,
            "written_sectors": 0,
        }
    )
    # Приложение AmneziaVPN: имя контейнера → его состояние (см. add_amnezia_app). Образы контейнеров по имени.
    apps: dict[str, AmneziaApp] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)
    container_reads: list[tuple[str, str]] = field(default_factory=list)  # (контейнер, путь) для каждого `cat`
    file_modes: dict[str, int] = field(default_factory=dict)  # права файлов, записанных через SFTP (всегда 0600)
    write_fails: set[str] = field(default_factory=set)  # SFTP-запись в эти пути хоста падает, файл не появляется
    write_attempts: list[str] = field(default_factory=list)  # все попытки SFTP-записи, в том числе неудачные
    copies: list["CopyRecord"] = field(default_factory=list)  # успешные `docker cp` в контейнеры
    # Контейнер шлюза (этап 24.1). Фейк не исполняет bash: start.sh «выполняется» как действие, если файл на хосте
    # равен тексту, который панель генерирует для раскладки (иначе контейнер падает, как от битого скрипта).
    container_commands: dict[str, str] = field(default_factory=dict)  # имя -> команда после образа в `docker run`
    container_sysctls: dict[str, list[str]] = field(default_factory=dict)  # имя -> флаги --sysctl
    container_ports: dict[str, list[int]] = field(default_factory=dict)  # имя -> опубликованные UDP-порты хоста
    host_busy_ports: set[int] = field(default_factory=set)  # порты, занятые процессами вне Docker (docker run падает)
    ip_rules: list[str] = field(default_factory=list)  # правила `ip rule show` в контейнере шлюза, по строке на правило
    firewall: set[str] = field(default_factory=set)  # правила iptables шлюза: "mss", "isolation"
    exit_interfaces: list[str] = field(default_factory=list)  # поднятые туннели выходов (кроме клиентского)
    # Выходы шлюза (24.2). Фейк не исполняет awg-quick: `up` по файлу `exits/gwx<N>.conf` разбирает конфиг и поднимает
    # интерфейс, `syncconf` перечитывает файл. Всё строго по константам панели; незнакомая команда - AssertionError.
    exit_configs: dict[str, str] = field(default_factory=dict)  # интерфейс -> конфиг, применённый к живому туннелю
    exit_handshakes: dict[int, int] = field(default_factory=dict)  # слот -> unix-время рукопожатия (нет записи - 0)
    exit_transfer: dict[int, tuple[int, int]] = field(default_factory=dict)  # слот -> (принято, отправлено) байт
    exit_up_fails: set[int] = field(default_factory=set)  # `awg-quick up` этих слотов падает (код 1)
    exit_down_fails: set[int] = field(default_factory=set)  # `awg-quick down` этих слотов падает
    # Назначение выходов клиентам (24.3). Фейк не исполняет iptables-restore: файл assign.rules разбирается строго по
    # формату панели (`*mangle`, объявление цепочки, правила MARK, `COMMIT`); иной формат - код 2, цепочка не меняется.
    assign_chain: dict[str, int] = field(default_factory=dict)  # адрес клиента -> метка в живой цепочке GW_ASSIGN
    assign_test_fails: bool = False  # `iptables-restore --test` падает
    assign_apply_fails: bool = False  # настоящее применение падает (после успешного --test)
    docker_run_fails: int = 0  # сколько ближайших `docker run` нашего контейнера упадут
    assign_applies: int = 0  # сколько раз цепочка заменена целиком
    exit_nat_cleanups: list[str] = field(default_factory=list)  # интерфейсы, чьё правило NAT снято вручную
    exit_sync_fails: set[int] = field(default_factory=set)  # strip/syncconf этих слотов падает
    start_script_runs: list[str] = field(default_factory=list)  # режимы запусков start.sh: "run" / "apply"

    def add_amnezia_app(
        self,
        container: str = "amnezia-awg2",
        peers: Sequence[AppPeer | str] = (),
        clients_table: str | dict[str, str] | None = None,
        running: bool = True,
        port: int = 34567,
        image: str | None = None,
    ) -> AmneziaApp:
        """Контейнер приложения AmneziaVPN с конфигом и clientsTable внутри. peers: AppPeer или публичный ключ
        (адреса выдаются подряд с 10.8.1.2). clients_table: готовый текст, словарь «ключ → имя» (формат-массив) или
        None — пустая таблица `[]`. Незапущенный контейнер: docker exec в нём падает, как в настоящем Docker."""
        layout = APP_LAYOUT[container]
        app_peers = [
            peer if isinstance(peer, AppPeer) else AppPeer(public_key=peer, allowed_ips=f"10.8.1.{index + 2}/32")
            for index, peer in enumerate(peers)
        ]
        keys = generate_secret_keys()
        config_path = f"{APP_CONFIG_DIR}/{layout['interface']}.conf"
        app = AmneziaApp(
            container=container,
            image=image or layout["image"],
            tool=layout["tool"],
            interface=layout["interface"],
            config_path=config_path,
            private_key=keys.private_key,
            port=port,
            peers=app_peers,
        )
        if clients_table is None:
            table = "[]"
        elif isinstance(clients_table, dict):
            table = clients_table_json(clients_table)
        else:
            table = clients_table
        app.files = {
            config_path: app.render_config(),
            APP_CLIENTS_TABLE: table,
            APP_PRIVATE_KEY_FILE: keys.private_key + "\n",
            APP_PSK_FILE: keys.psk + "\n",
        }
        self.apps[container] = app
        self.containers[container] = "running" if running else "exited"
        self.mounts[container] = [f"/var/lib/docker/volumes/{container}/_data"]  # не папка панели
        self.images[container] = app.image
        return app

    def host_metrics_output(self) -> str:
        h = self.host_stats
        return (
            f"@uptime\n{h['uptime']} 0.00\n"
            f"@stat\ncpu  {int(h['cpu_busy'])} 0 0 {int(h['cpu_idle'])} 0 0 0 0 0 0\n"
            "@loadavg\n0.10 0.10 0.10 1/100 1\n"
            "@meminfo\nMemTotal:        1000000 kB\nMemAvailable:     600000 kB\n"
            "@df\nFilesystem 1-blocks Used Available Capacity Mounted on\n"
            "/dev/vda1 10000000000 4000000000 6000000000 40% /\n"
            "@route\ndefault via 203.0.113.1 dev eth0\n"
            "@netdev\nInter-| header\n face | header\n"
            f"  eth0: {int(h['rx'])} 0 0 0 0 0 0 0 {int(h['tx'])} 0 0 0 0 0 0 0\n"
            f"@diskstats\n 253 0 vda 0 0 {int(h['read_sectors'])} 0 0 0 {int(h['written_sectors'])} 0 0 0 0\n"
        )

    def authorized_blobs(self) -> set[str]:
        """blob'ы из /root/.ssh/authorized_keys фейка (2-е поле строки; строки с options не поддерживаются)."""
        found: set[str] = set()
        for line in self.files.get(AUTHORIZED_KEYS, "").splitlines():
            fields = line.split()
            if len(fields) >= 2 and not line.lstrip().startswith("#"):
                found.add(fields[1])
        return found

    def add_foreign_container(self, state: str = "running", source: str = FOREIGN_MOUNT_SOURCE) -> None:
        """Контейнер `amnezia-awg`, созданный не панелью (приложением AmneziaVPN): конфиг внутри тома Docker,
        папки панели среди монтирований нет."""
        self.containers[self.layout.container_name] = state
        self.mounts[self.layout.container_name] = [source]

    def mutating_commands(self) -> list[str]:
        """Команды из журнала, которые меняют сервер (или исполняются внутри контейнера). Чтение — docker ps/inspect,
        test -f, grep, uname — сюда не входит."""
        return [c for c in self.commands if not " ".join(c.split()).startswith(_READ_ONLY_PREFIXES)]

    def changing_commands(self) -> list[str]:
        """Как mutating_commands, но чтение внутри контейнера не считается изменением: `docker exec <контейнер> cat
        /opt/amnezia/awg/<файл>` и `docker exec <контейнер> wg|awg show <интерфейс> latest-handshakes`. Любой иной
        docker exec остаётся изменяющим. Для провайдеров чужого контейнера (приложение AmneziaVPN)."""
        return [c for c in self.mutating_commands() if not _READ_ONLY_EXEC.fullmatch(" ".join(c.split()))]

    def unexpected_changing_commands(self) -> list[str]:
        """changing_commands() без команд протокола записи в приложение (23.3): `docker cp <stage> C:<путь>`,
        `rm -f <stage>`, `docker exec C bash -c '<утилита> syncconf …'`. Всё, что осталось, — лишнее."""
        return [c for c in self.changing_commands() if not _APP_WRITE_COMMANDS.fullmatch(" ".join(c.split()))]

    def stage_files(self) -> list[str]:
        """Промежуточные файлы на хосте. После операции записи их не должно остаться: там бывает приватный ключ."""
        return sorted(path for path in self.files if path.startswith(f"{STAGE_DIR}/"))

    @property
    def config_file(self) -> str:
        return self.layout.host_config_file

    @property
    def running(self) -> bool:
        return self.containers.get(self.layout.container_name) == "running"

    def config_peer_keys(self) -> set[str]:
        return {peer.public_key for peer in list_peers(self.files[self.config_file])}

    def _port(self) -> str:
        return interface_values(self.files[self.config_file])["ListenPort"]

    def _start(self) -> None:
        # Как awg-quick up: поднимает интерфейс по конфигу и выполняет PostUp.
        name = self.layout.container_name
        self.containers[name] = "running"
        self.interface_peers = self.config_peer_keys()
        self.nat_rule = self.layout.nat_post_up in self.files.get(self.config_file, "")
        if self.container_commands.get(name, "").endswith("/start.sh"):  # контейнер шлюза: bash <путь>/start.sh
            self.exit_interfaces, self.exit_configs = [], {}  # новый запуск контейнера: интерфейсов ещё нет
            self.assign_chain, self.firewall = {}, set()
            self.ip_rules = [rule for rule in self.ip_rules if not rule.startswith(f"{EXIT_RULE_PRIORITY}:")]
            if not self._run_start_script("run"):
                self.containers[name] = "exited"  # скрипт упал - контейнер вышел (restart policy в фейке не крутится)
                self.interface_peers, self.nat_rule = set(), False

    def _published(self, name: str) -> str:
        """Поле Ports из `docker ps`: порты работающего контейнера (у остановленного поле пустое, как в Docker)."""
        if self.containers.get(name) != "running":
            return ""
        ports = self.container_ports.get(name, [])
        return ", ".join(f"0.0.0.0:{port}->{port}/udp, [::]:{port}->{port}/udp" for port in ports)

    def _run_start_script(self, mode: str) -> bool:
        """start.sh шлюза: правила маршрутизации и экрана. False - скрипт не выполнился (нет файла или он не тот)."""
        layout = self.layout
        script = self.files.get(f"{layout.host_config_dir}/start.sh")
        if script is None or script != render_start_script(layout):
            return False
        self.start_script_runs.append(mode)
        subnet = layout.tunnel_subnet
        rules = (
            f"{RU_RULE_PRIORITY}:\tfrom {subnet} lookup {RU_TABLE}",
            f"{KILL_SWITCH_PRIORITY}:\tfrom {subnet} prohibit",
        )
        self.ip_rules += [rule for rule in rules if rule not in self.ip_rules]
        self.firewall |= {"isolation", "mss", "assign", "assign_hooks", "ct_guard"}
        saved = self.files.get(assign_host_file(layout))
        if saved is not None and (parsed := _parse_assign_rules(saved)) is not None:
            self.assign_chain = parsed  # start.sh: iptables-restore --noflush < assign.rules
        # Туннели выходов: каждый exits/*.conf, которого ещё нет среди поднятых; упавший контейнер не роняет.
        for path in sorted(self.files):
            slot = self._exit_slot_of_host_file(path)
            if slot is not None and exit_interface(slot) not in self.exit_interfaces and slot not in self.exit_up_fails:
                self._bring_up_exit(slot)
        return True

    def _exit_slot_of_host_file(self, path: str) -> int | None:
        match = re.fullmatch(rf"{re.escape(exit_host_dir(self.layout))}/(gwx[1-9]\d*)\.conf", path)
        return exit_slot_from_interface(match.group(1)) if match else None

    def _bring_up_exit(self, slot: int) -> CommandResult:
        """`awg-quick up` над файлом выхода: интерфейс поднят, если конфиг разбирается (иначе код 1)."""
        name = exit_interface(slot)
        text = self.files.get(exit_host_file(self.layout, slot))
        if text is None:
            return _fail(f"`{exit_container_file(self.layout, slot)}' does not exist", 1)
        if name in self.exit_interfaces:
            return _fail(f"awg-quick: `{name}' already exists", 1)
        try:
            interface, peer = client_config_sections(text)
            valid = (
                is_valid_public_key(interface["PrivateKey"])
                and is_valid_public_key(peer["PublicKey"])
                and interface["Table"] == str(exit_table(slot))
                and "Endpoint" in peer
            )
        except KeyError:
            valid = False
        if not valid:
            return _fail("Configuration parsing error", 1)
        self.exit_interfaces.append(name)
        self.exit_configs[name] = text
        rule = f"{EXIT_RULE_PRIORITY}:\tfrom all fwmark 0x{exit_mark(slot):x} lookup {exit_mark(slot)}"
        if exit_rule_up(slot) in text and rule not in self.ip_rules:
            self.ip_rules.append(rule)  # PostUp туннеля
        return _ok()

    def _drop_exit_rule(self, slot: int) -> None:
        """Правило метки уходит вместе с интерфейсом (PostDown выполняет `awg-quick down`)."""
        rule = f"{EXIT_RULE_PRIORITY}:\tfrom all fwmark 0x{exit_mark(slot):x} lookup {exit_mark(slot)}"
        self.ip_rules = [item for item in self.ip_rules if item != rule]

    def run(self, command: str) -> CommandResult:
        layout = self.layout
        container = layout.container_name
        self.commands.append(command)
        cmd = " ".join(command.split())  # docker run в коде многострочный — схлопываем пробелы
        if cmd == "uname -s":
            return _ok("Linux\n")
        if cmd == "uname -a":  # test-connection
            return _ok("Linux fake 6.8.0 #1 SMP x86_64 GNU/Linux\n")
        if cmd == "id -u":
            return _ok("0\n")
        if cmd == "command -v docker":
            return _ok("/usr/bin/docker\n") if self.docker_installed else _fail(code=1)
        if cmd.startswith("apt-get update && apt-get install -y docker.io"):
            self.docker_installed = True
            return _ok()
        if cmd == "systemctl is-active docker":
            return _ok("active\n") if self.docker_active else CommandResult("inactive\n", "", 3)
        if cmd in ("systemctl start docker", "systemctl enable docker"):
            self.docker_active = True
            return _ok()
        if cmd == "docker --version":
            return _ok("Docker version 27.0.3, build fake\n") if self.docker_installed else _fail("not found", 127)
        if cmd == INTERNET_PROBE_COMMAND:
            return _ok("0.042000") if self.internet else _fail(code=28)
        if cmd == " ".join(HOST_METRICS_COMMAND.split()):
            return _ok(self.host_metrics_output())
        if match := re.fullmatch(r"test -d (\S+)", cmd):
            path = match.group(1)
            exists = path in self.dirs or any(name.startswith(f"{path}/") for name in self.files)
            return _ok() if exists else _fail(code=1)
        if match := re.fullmatch(r"test -f (\S+)", cmd):
            return _ok() if match.group(1) in self.files else _fail(code=1)
        if cmd == DOCKER_PUBLISHED_PORTS:
            if self.ps_fails:
                return _fail("Cannot connect to the Docker daemon", 1)
            return _ok("".join(f"{self._published(name)}\n" for name in self.containers))
        if match := _PS_RE.fullmatch(cmd):
            if self.ps_fails:
                return _fail("Cannot connect to the Docker daemon", 1)
            return self._docker_ps(match.group(1), match.group(2))
        if match := _INSPECT_RUNNING_RE.fullmatch(cmd):
            if match.group(1) not in self.containers:
                return _fail("No such object", 1)
            return _ok("true\n" if self.containers[match.group(1)] == "running" else "false\n")
        if cmd == " ".join(sysctls_command(layout).split()):
            if container not in self.containers:
                return _fail("No such object", 1)
            return _ok(" ".join(self.container_sysctls.get(container, [])) + chr(10))
        if match := _INSPECT_MOUNTS_RE.fullmatch(cmd):
            name = match.group(1)
            if name not in self.containers:
                return _fail("No such object", 1)
            default = [layout.host_config_dir] if name == container else []
            # Формат шаблона `{{.Source}} ` на каждое монтирование: источники через пробел, с хвостовым пробелом.
            return _ok("".join(f"{source} " for source in self.mounts.get(name, default)) + "\n")
        if match := re.fullmatch(r"grep 'ListenPort' (\S+) \| cut -d' ' -f3", cmd):
            # Как настоящий пайп: код возврата — от cut (0), даже если файла нет и вывод пустой.
            content = self.files.get(match.group(1), "")
            port = next((line.split()[2] for line in content.splitlines() if line.startswith("ListenPort")), "")
            return _ok(port + "\n" if port else "")
        if cmd == f"docker inspect -f '{{{{.HostConfig.RestartPolicy.Name}}}}' {container}":
            if container not in self.containers:
                return _fail("No such object", 1)
            return _ok(f"{self.restart_policy}\n")
        if cmd == f"docker update --restart unless-stopped {container}":
            if container not in self.containers:
                return _fail("No such container", 1)
            self.restart_policy = "unless-stopped"
            return _ok(f"{container}\n")
        if cmd.startswith(f"docker run -d --name {container} "):
            if self.docker_run_fails > 0:  # сбой создания контейнера (демон, образ, порт)
                self.docker_run_fails -= 1
                return _fail("docker: Error response from daemon: cannot start", 125)
            if container in self.containers:  # как настоящий docker: имя занято
                return _fail(f'Conflict. The container name "/{container}" is already in use', 125)
            requested = {int(port) for port in re.findall(r" -p (\d+):\d+/udp", cmd)}
            taken = {
                port
                for name, state in self.containers.items()
                if state == "running"
                for port in self.container_ports.get(name, [])
            } | self.host_busy_ports
            if requested & taken:  # как настоящий docker: порт хоста уже опубликован другим контейнером
                return _fail("Bind for 0.0.0.0:%d failed: port is already allocated" % min(requested & taken), 125)
            self.mounts[container] = re.findall(r" -v (\S+?):\S+", cmd)
            self.restart_policy = "unless-stopped" if " --restart unless-stopped " in cmd else "no"
            self.container_sysctls[container] = re.findall(r" --sysctl (\S+)", cmd)
            self.container_ports[container] = [int(port) for port in re.findall(r" -p (\d+):\d+/udp", cmd)]
            self.container_commands[container] = cmd.split(" amneziavpn/amneziawg-go@", 1)[-1].split(" ", 1)[-1]
            self._start()
            return _ok("container-id\n")
        if cmd == f"docker start {container}":
            self._start()
            return _ok()
        if cmd == f"docker stop {container} && docker rm {container}":
            self.containers.pop(container, None)
            self.mounts.pop(container, None)
            self.container_commands.pop(container, None)
            self.container_sysctls.pop(container, None)
            self.container_ports.pop(container, None)
            self.interface_peers = set()
            self.nat_rule = False
            self.ip_rules, self.firewall, self.exit_interfaces, self.exit_configs = [], set(), [], {}
            self.assign_chain = {}
            return _ok()
        if cmd == f"rm -rf {layout.host_config_dir}":
            self.files = {
                path: text for path, text in self.files.items() if not path.startswith(layout.host_config_dir)
            }
            self.dirs = {path: mode for path, mode in self.dirs.items() if not path.startswith(layout.host_config_dir)}
            return _ok()
        if match := re.fullmatch(rf"docker port {container} (\d+)/udp", cmd):
            if not self.port_published:
                return _fail("no public port published", 1)
            return _ok(f"0.0.0.0:{match.group(1)}\n[::]:{match.group(1)}\n")
        if match := re.fullmatch(r"docker cp (\S+) ([\w.-]+):(\S+)", cmd):
            return self._docker_cp(*match.groups())
        if cmd == f"mkdir -p {_STAGE_PARENT}":
            return _ok()
        if match := re.fullmatch(r"rm -f (\S+)", cmd):
            # Файлы шлюза (откат первой установки) - только в шлюзовой раскладке; конфиг обычного протокола - нет.
            gateway_files = (
                set()
                if layout == DEFAULT_LAYOUT
                else {layout.host_config_file, f"{layout.host_config_dir}/start.sh", assign_host_file(layout)}
            )
            exit_file = self._exit_slot_of_host_file(match.group(1)) is not None
            if not _STAGE_FILE.fullmatch(match.group(1)) and match.group(1) not in gateway_files and not exit_file:
                # удалять можно только промежуточные файлы панели, файлы шлюза и файлы его выходов
                raise AssertionError(f"FakeServer: rm -f outside the stage folder: {command!r}")
            pattern = match.group(1)  # как настоящий rm -f: нет файла — не ошибка; `*` раскрывает оболочка
            for path in [p for p in self.files if fnmatch.fnmatchcase(p, pattern)]:
                self.files.pop(path)
            self.file_modes.pop(match.group(1), None)
            return _ok()
        if match := re.fullmatch(r"docker exec (\S+) (.+)", cmd):
            if (app := self.apps.get(match.group(1))) is not None:
                return self._app_exec(app, match.group(2))
        if cmd.startswith(f"docker exec {container} "):
            if not self.running:
                return _fail(f"container {container} is not running", 1)
            return self._exec(cmd.removeprefix(f"docker exec {layout.container_name} "))
        raise AssertionError(f"FakeServer: unexpected command: {command!r}")

    def _docker_ps(self, filter_arg: str, template: str) -> CommandResult:
        # Настоящая семантика фильтра docker: name=<регулярное выражение> ищется в имени с ведущим «/».
        key, _, pattern = shlex.split(filter_arg)[0].partition("=")
        assert key == "name", filter_arg
        lines = []
        for name, state in self.containers.items():
            if re.search(pattern, f"/{name}"):
                values = {
                    "State": state,
                    "Status": "Up 5 minutes" if state == "running" else "Exited (0) 1 minute ago",
                    "Image": self.images.get(name, "amneziavpn/amneziawg-go:latest"),
                    "Names": name,
                }

                def substitute(match: re.Match[str], values: dict[str, str] = values) -> str:
                    if match.group(1) not in values:
                        raise AssertionError(f"FakeServer: unsupported docker ps field {match.group(0)!r}")
                    return values[match.group(1)]

                lines.append(_PS_FIELD_RE.sub(substitute, template.replace("\\t", "\t")))
        return _ok("".join(f"{line}\n" for line in lines))

    def _docker_cp(self, src: str, container: str, dst: str) -> CommandResult:
        """`docker cp <файл хоста> <контейнер>:<путь>`: только из промежуточной папки панели в конфиг или таблицу
        приложения, и только в «свои» промежуточные файлы контейнера. Остальное — AssertionError (чужие пути,
        чужие контейнеры — в том числе контейнер самой панели)."""
        app = self.apps.get(container)
        if app is None:
            raise AssertionError(f"FakeServer: docker cp into {container!r}, which is not an AmneziaVPN app container")
        expected = {app.config_path: stage_conf(container), APP_CLIENTS_TABLE: stage_table(container)}
        if dst not in expected:
            raise AssertionError(f"FakeServer: docker cp into unexpected path {dst!r}")
        if src != expected[dst]:
            raise AssertionError(f"FakeServer: docker cp from {src!r} into {dst!r}")
        if self.containers.get(container) != "running":
            return _fail(f"Error response from daemon: container {container} is not running", 1)
        if src not in self.files:
            return _fail(f"Error: Could not find the file {src} in the host", 1)
        if dst in app.docker_cp_fails:
            return _fail("Error response from daemon: copy failed", 1)
        app.files[dst] = self.files[src]
        self.copies.append(CopyRecord(src, container, dst, self.file_modes.get(src), self.files[src]))
        return _ok()

    def _apply(self, app: AmneziaApp, tool: str, interface: str, strip_tool: str, path: str) -> CommandResult:
        """`<утилита> syncconf <интерфейс> <(<утилита>-quick strip <конфиг>)`: живые peer'ы берутся из конфига."""
        if strip_tool != tool or path != app.config_path:
            raise AssertionError(f"FakeServer: syncconf with {strip_tool!r} over {path!r}")
        if tool != app.tool:  # в контейнере другой утилиты нет
            return _fail(f'exec: "{tool}": executable file not found in $PATH', 126)
        if interface != app.interface or not app.interface_up:
            return _fail(f"Unable to access interface: No such device ({interface})", 1)
        if app.on_apply is not None:
            app.on_apply()
        if app.apply_fails:
            return _fail("Configuration parsing error", 1)  # настоящий wg цитирует строку, фейк — нет
        try:
            app.peers = _parse_app_config(app.files.get(path, ""), app.peers)
        except ValueError:
            return _fail("Configuration parsing error", 1)
        app.applies += 1
        return _ok()

    def _app_exec(self, app: AmneziaApp, inner: str) -> CommandResult:
        """docker exec в контейнере приложения AmneziaVPN: чтение (файл, рукопожатия) и применение конфига."""
        if self.containers.get(app.container) != "running":
            return _fail(f"Error response from daemon: container {app.container} is not running", 1)
        if match := re.fullmatch(r"bash -c '" + _APPLY_SCRIPT.format(strip=r"(\S+)") + "'", inner):
            strip_tool, path, tool, interface = match.groups()
            return self._apply(app, tool, interface, strip_tool, path)
        if match := re.fullmatch(r"cat (\S+)", inner):
            self.container_reads.append((app.container, match.group(1)))
            app.read_counts[match.group(1)] = count = app.read_counts.get(match.group(1), 0) + 1
            if app.on_read is not None:
                app.on_read(match.group(1), count)
            if match.group(1) not in app.files:
                return _fail(f"cat: can't open '{match.group(1)}': No such file or directory", 1)
            return _ok(app.files[match.group(1)])
        if match := re.fullmatch(r"(wg|awg) show (\S+) latest-handshakes", inner):
            if match.group(1) != app.tool:  # в контейнере другой утилиты нет
                return _fail(f'exec: "{match.group(1)}": executable file not found in $PATH', 126)
            if match.group(2) != app.interface or not app.interface_up:
                return _fail(f"Unable to access interface: No such device ({match.group(2)})", 1)
            return _ok(app.handshakes_output())
        raise AssertionError(f"FakeServer: unexpected docker exec in {app.container}: {inner!r}")

    def _assign_apply(self) -> CommandResult:
        """`iptables-restore --test --noflush < assign.rules && iptables-restore --noflush < assign.rules`."""
        text = self.files.get(assign_host_file(self.layout))
        parsed = _parse_assign_rules(text) if text is not None else None
        if parsed is None or self.assign_test_fails:
            return _fail("iptables-restore: line 3 failed", 2)
        if self.assign_apply_fails:
            return _fail("iptables-restore: apply failed", 1)
        self.assign_chain = parsed  # объявление цепочки очищает её: каждое применение - полная замена
        self.firewall.add("assign")
        self.assign_applies += 1
        return _ok()

    def _exit_quick(self, action: str, container_path: str, inner: str) -> CommandResult:
        """`awg-quick up|down <exits/gwx<N>.conf>`: путь и команда строго такие, как их собирает провайдер."""
        match = re.fullmatch(
            rf"{re.escape(self.layout.container_config_dir)}/exits/(gwx[1-9]\d*)\.conf", container_path
        )
        slot = exit_slot_from_interface(match.group(1)) if match else None
        if slot is None:
            raise AssertionError(f"FakeServer: awg-quick on an unexpected path: {inner!r}")
        name = exit_interface(slot)
        if action == "up":
            if slot in self.exit_up_fails:
                return _fail("awg-quick: failed to bring up", 1)
            return self._bring_up_exit(slot)
        if name not in self.exit_interfaces or slot in self.exit_down_fails:
            return _fail(f"awg-quick: `{name}' is not a WireGuard interface", 1)
        self.exit_interfaces.remove(name)
        text = self.exit_configs.pop(name, "")
        if exit_rule_down(slot) in text:
            self._drop_exit_rule(slot)
        return _ok()

    def _exit_peer_key(self, name: str) -> str:
        _, peer = client_config_sections(self.exit_configs[name])
        return peer["PublicKey"]

    def _exit_show(self, name: str, field_name: str, inner: str) -> CommandResult:
        slot = exit_slot_from_interface(name)
        if slot is None:
            raise AssertionError(f"FakeServer: awg show of an unexpected interface: {inner!r}")
        if name not in self.exit_interfaces:
            return _fail(f"Unable to access interface: No such device ({name})", 1)
        key = self._exit_peer_key(name)
        if field_name == "latest-handshakes":
            return _ok(f"{key}\t{self.exit_handshakes.get(slot, 0)}\n")
        rx, tx = self.exit_transfer.get(slot, (0, 0))
        return _ok(f"{key}\t{rx}\t{tx}\n")

    def _exit_syncconf(self, name: str, inner: str) -> CommandResult:
        """strip -> временный файл `&&` syncconf (безопасный вариант 23.3): живой конфиг берётся из файла."""
        slot = exit_slot_from_interface(name)
        if slot is None or inner != exit_sync_command(self.layout, slot).removeprefix(
            f"docker exec {self.layout.container_name} "
        ):
            raise AssertionError(f"FakeServer: unexpected exit sync command: {inner!r}")
        text = self.files.get(exit_host_file(self.layout, slot))
        if name not in self.exit_interfaces or text is None or slot in self.exit_sync_fails:
            return _fail("Configuration parsing error", 1)
        self.exit_configs[name] = text
        return _ok()

    def _exec(self, inner: str) -> CommandResult:
        layout = self.layout
        iface = layout.interface_name
        if match := re.fullmatch(r"cat (\S+)", inner):  # в нашем контейнере файлов приложения AmneziaVPN нет
            self.container_reads.append((layout.container_name, match.group(1)))
            return _fail(f"cat: can't open '{match.group(1)}': No such file or directory", 1)
        if re.fullmatch(r"wg show \S+ latest-handshakes", inner):  # у нашего образа только awg
            return _fail('exec: "wg": executable file not found in $PATH', 126)
        if inner == f"sh -c '{layout.nat_post_up}'":
            self.nat_rule = True
            return _ok()
        if inner == f"awg show {iface} public-key":
            private_key = interface_values(self.files[self.config_file])["PrivateKey"]
            return _ok(public_key_from_private(private_key) + "\n")
        if inner == IP_RULE_SHOW:
            rules = "".join(f"{rule}\n" for rule in sorted(self.ip_rules, key=lambda item: int(item.split(":")[0])))
            return _ok(f"0:\tfrom all lookup local\n{rules}32766:\tfrom all lookup main\n")
        if inner == AWG_SHOW_INTERFACES:
            return _ok(" ".join([iface, *self.exit_interfaces]) + "\n")
        if inner == f"bash {layout.container_config_dir}/start.sh apply":
            return _ok() if self._run_start_script("apply") else _fail("bash: start.sh: No such file", 127)
        if inner == assign_apply_command(layout).removeprefix(f"docker exec {layout.container_name} "):
            return self._assign_apply()
        if inner == "iptables -t mangle -S PREROUTING":
            lines = ["-P PREROUTING ACCEPT"]
            if "assign_hooks" in self.firewall:
                head = f"-A PREROUTING -i {layout.interface_name}"
                masks = "--nfmask 0xffffffff --ctmask 0xffffffff"
                lines += [
                    f"{head} -j CONNMARK --restore-mark {masks}",
                    f"{head} -m mark --mark 0x0 -j GW_ASSIGN",
                    f"{head} -j CONNMARK --save-mark {masks}",
                ]
            return _ok("".join(f"{line}{chr(10)}" for line in lines))
        if inner == assign_show_command(layout).removeprefix(f"docker exec {layout.container_name} "):
            if "assign" not in self.firewall:
                return _fail("iptables: No chain/target/match by that name.", 1)
            rules = "".join(
                f"-A GW_ASSIGN -s {address}/32 -j MARK --set-xmark 0x{mark:x}/0xffffffff\n"
                for address, mark in sorted(self.assign_chain.items())
            )
            return _ok(f"-N GW_ASSIGN\n{rules}")
        if match := re.fullmatch(r"awg-quick (up|down) (\S+)", inner):
            return self._exit_quick(match.group(1), match.group(2), inner)
        if match := re.fullmatch(r"iptables -t nat -D POSTROUTING -o (gwx\S+) -j MASQUERADE \|\| true", inner):
            if exit_slot_from_interface(match.group(1)) is None:
                raise AssertionError(f"FakeServer: NAT cleanup of a non-exit interface: {inner!r}")
            self.exit_nat_cleanups.append(match.group(1))
            return _ok()
        if match := re.fullmatch(r"ip link delete dev (\S+)", inner):
            slot = exit_slot_from_interface(match.group(1))
            if slot is None:
                raise AssertionError(f"FakeServer: ip link delete of a non-exit interface: {inner!r}")
            if match.group(1) not in self.exit_interfaces:
                return _fail(f'Cannot find device "{match.group(1)}"', 1)
            self.exit_interfaces.remove(match.group(1))
            self.exit_configs.pop(
                match.group(1), None
            )  # ссылку удалили напрямую: PostDown не выполнялся, правило метки живо
            return _ok()
        if match := re.fullmatch(r"awg show (gwx\S+) (latest-handshakes|transfer)", inner):
            return self._exit_show(match.group(1), match.group(2), inner)
        if match := re.fullmatch(r"sh -c 'umask 077; awg-quick strip (\S+) > /tmp/(gwx\S+)\.stripped .*", inner):
            return self._exit_syncconf(match.group(2), inner)
        if inner == f"awg show {iface} listen-port":
            return _ok(self._port() + "\n")
        if inner == f"awg show {iface} latest-handshakes":
            return _ok("".join(f"{key}\t{self.handshakes.get(key, 0)}\n" for key in sorted(self.interface_peers)))
        stripped = f"/tmp/{iface}.stripped"
        if inner == (
            f"sh -c 'awg-quick strip {layout.container_config_file} > {stripped}"
            f" && awg syncconf {iface} {stripped}; rc=$?; rm -f {stripped}; exit $rc'"
        ):
            self.interface_peers = self.config_peer_keys()
            return _ok()
        if inner == f"iptables -t nat -C {layout.nat_rule}":
            return _ok() if self.nat_rule else _fail("Bad rule (does a matching rule exist in that chain?)", 1)
        raise AssertionError(f"FakeServer: unexpected docker exec: {inner!r}")


class FakeSSHClient:
    """Тот же интерфейс, что у SSHClient, но вместо сети — FakeServer."""

    def __init__(self, server: FakeServer, private_key: str | None = None):
        self.server = server
        self.host = server.host
        self._private_key = private_key
        self.connected = False

    async def __aenter__(self) -> "FakeSSHClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if not self.server.reachable:
            raise SSHConnectionError(f"Connection error: host={self.host}, port=22")
        if self._private_key is not None:
            # Как настоящий sshd: пускает по ключу, только если он записан в authorized_keys и вход по ключам включён.
            if not self.server.key_auth_enabled or key_blob(self._private_key) not in self.server.authorized_blobs():
                raise SSHAuthError("Incorrect username or password/secret_key")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def execute(self, command: str, timeout: int | None = None) -> CommandResult:
        assert self.connected, "execute() before connect()"
        if self._private_key is not None and self.server.on_key_command is not None:
            await self.server.on_key_command(command)
        return self.server.run(command)

    async def read_file(self, remote_path: str) -> str:
        assert self.connected, "read_file() before connect()"
        self.server.reads.append(remote_path)
        if remote_path not in self.server.files:
            raise SSHConnectionError(f"File read: host={self.host}, path={remote_path}")
        return self.server.files[remote_path]

    async def write_file(self, remote_path: str, content: str) -> None:
        assert self.connected, "write_file() before connect()"
        self.server.write_attempts.append(remote_path)
        if remote_path in self.server.write_fails:
            raise SSHConnectionError(f"File write: host={self.host}, path={remote_path}")
        self.server.writes.append(remote_path)
        self.server.files[remote_path] = content
        self.server.file_modes[remote_path] = 0o600  # настоящий write_file создаёт файл сразу с 0600

    async def home_dir(self) -> str:
        assert self.connected, "home_dir() before connect()"
        return HOME_DIR

    async def ensure_private_dir(self, remote_path: str) -> None:
        assert self.connected, "ensure_private_dir() before connect()"
        self.server.dirs[remote_path] = 0o700

    async def read_file_optional(self, remote_path: str) -> str | None:
        assert self.connected, "read_file_optional() before connect()"
        self.server.reads.append(remote_path)
        return self.server.files.get(remote_path)
