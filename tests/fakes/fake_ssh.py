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
    generate_secret_keys,
    interface_values,
    is_valid_public_key,
    list_peers,
    public_key_from_private,
)
from infrastructure.vpn.config import (
    CONFIG_FILE_NAME,
    CONTAINER_NAME,
    HOST_CONFIG_DIR,
    INTERFACE_NAME,
    INTERNET_PROBE_COMMAND,
    NAT_POST_UP,
    NAT_RULE,
)

CONFIG_FILE = f"{HOST_CONFIG_DIR}/{CONFIG_FILE_NAME}"
_PS_RE = re.compile(r"docker ps -a --filter (\S+) --format '([^']*)'")
_PS_FIELD_RE = re.compile(r"\{\{\.(\w+)\}\}")
_INSPECT_RUNNING_RE = re.compile(r"docker inspect (?:--type container )?-f '\{\{\.State\.Running\}\}' (\S+)")
_INSPECT_MOUNTS_RE = re.compile(
    r"docker inspect --type container -f '\{\{range \.Mounts\}\}\{\{\.Source\}\} \{\{end\}\}' (\S+)"
)
_EXEC = f"docker exec {CONTAINER_NAME} "

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
        self.containers[CONTAINER_NAME] = state
        self.mounts[CONTAINER_NAME] = [source]

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
    def running(self) -> bool:
        return self.containers.get(CONTAINER_NAME) == "running"

    def config_peer_keys(self) -> set[str]:
        return {peer.public_key for peer in list_peers(self.files[CONFIG_FILE])}

    def _port(self) -> str:
        return interface_values(self.files[CONFIG_FILE])["ListenPort"]

    def _start(self) -> None:
        # Как awg-quick up: поднимает интерфейс по конфигу и выполняет PostUp.
        self.containers[CONTAINER_NAME] = "running"
        self.interface_peers = self.config_peer_keys()
        self.nat_rule = NAT_POST_UP in self.files.get(CONFIG_FILE, "")

    def run(self, command: str) -> CommandResult:
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
        if match := re.fullmatch(r"test -f (\S+)", cmd):
            return _ok() if match.group(1) in self.files else _fail(code=1)
        if match := _PS_RE.fullmatch(cmd):
            if self.ps_fails:
                return _fail("Cannot connect to the Docker daemon", 1)
            return self._docker_ps(match.group(1), match.group(2))
        if match := _INSPECT_RUNNING_RE.fullmatch(cmd):
            if match.group(1) not in self.containers:
                return _fail("No such object", 1)
            return _ok("true\n" if self.containers[match.group(1)] == "running" else "false\n")
        if match := _INSPECT_MOUNTS_RE.fullmatch(cmd):
            name = match.group(1)
            if name not in self.containers:
                return _fail("No such object", 1)
            default = [HOST_CONFIG_DIR] if name == CONTAINER_NAME else []
            # Формат шаблона `{{.Source}} ` на каждое монтирование: источники через пробел, с хвостовым пробелом.
            return _ok("".join(f"{source} " for source in self.mounts.get(name, default)) + "\n")
        if match := re.fullmatch(r"grep 'ListenPort' (\S+) \| cut -d' ' -f3", cmd):
            # Как настоящий пайп: код возврата — от cut (0), даже если файла нет и вывод пустой.
            content = self.files.get(match.group(1), "")
            port = next((line.split()[2] for line in content.splitlines() if line.startswith("ListenPort")), "")
            return _ok(port + "\n" if port else "")
        if cmd == f"docker inspect -f '{{{{.HostConfig.RestartPolicy.Name}}}}' {CONTAINER_NAME}":
            if CONTAINER_NAME not in self.containers:
                return _fail("No such object", 1)
            return _ok(f"{self.restart_policy}\n")
        if cmd == f"docker update --restart unless-stopped {CONTAINER_NAME}":
            if CONTAINER_NAME not in self.containers:
                return _fail("No such container", 1)
            self.restart_policy = "unless-stopped"
            return _ok(f"{CONTAINER_NAME}\n")
        if cmd.startswith(f"docker run -d --name {CONTAINER_NAME} "):
            if CONTAINER_NAME in self.containers:  # как настоящий docker: имя занято
                return _fail(f'Conflict. The container name "/{CONTAINER_NAME}" is already in use', 125)
            self.mounts[CONTAINER_NAME] = re.findall(r" -v (\S+?):\S+", cmd)
            self.restart_policy = "unless-stopped" if " --restart unless-stopped " in cmd else "no"
            self._start()
            return _ok("container-id\n")
        if cmd == f"docker start {CONTAINER_NAME}":
            self._start()
            return _ok()
        if cmd == f"docker stop {CONTAINER_NAME} && docker rm {CONTAINER_NAME}":
            self.containers.pop(CONTAINER_NAME, None)
            self.mounts.pop(CONTAINER_NAME, None)
            self.interface_peers = set()
            self.nat_rule = False
            return _ok()
        if cmd == f"rm -rf {HOST_CONFIG_DIR}":
            self.files = {path: text for path, text in self.files.items() if not path.startswith(HOST_CONFIG_DIR)}
            return _ok()
        if match := re.fullmatch(rf"docker port {CONTAINER_NAME} (\d+)/udp", cmd):
            if not self.port_published:
                return _fail("no public port published", 1)
            return _ok(f"0.0.0.0:{match.group(1)}\n[::]:{match.group(1)}\n")
        if match := re.fullmatch(r"docker cp (\S+) ([\w.-]+):(\S+)", cmd):
            return self._docker_cp(*match.groups())
        if cmd == f"mkdir -p {_STAGE_PARENT}":
            return _ok()
        if match := re.fullmatch(r"rm -f (\S+)", cmd):
            if not _STAGE_FILE.fullmatch(match.group(1)):  # удалять можно только промежуточные файлы панели
                raise AssertionError(f"FakeServer: rm -f outside the stage folder: {command!r}")
            pattern = match.group(1)  # как настоящий rm -f: нет файла — не ошибка; `*` раскрывает оболочка
            for path in [p for p in self.files if fnmatch.fnmatchcase(p, pattern)]:
                self.files.pop(path)
            self.file_modes.pop(match.group(1), None)
            return _ok()
        if match := re.fullmatch(r"docker exec (\S+) (.+)", cmd):
            if (app := self.apps.get(match.group(1))) is not None:
                return self._app_exec(app, match.group(2))
        if cmd.startswith(_EXEC):
            if not self.running:
                return _fail(f"container {CONTAINER_NAME} is not running", 1)
            return self._exec(cmd.removeprefix(_EXEC))
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

    def _exec(self, inner: str) -> CommandResult:
        if match := re.fullmatch(r"cat (\S+)", inner):  # в нашем контейнере файлов приложения AmneziaVPN нет
            self.container_reads.append((CONTAINER_NAME, match.group(1)))
            return _fail(f"cat: can't open '{match.group(1)}': No such file or directory", 1)
        if re.fullmatch(r"wg show \S+ latest-handshakes", inner):  # у нашего образа только awg
            return _fail('exec: "wg": executable file not found in $PATH', 126)
        if inner == f"sh -c '{NAT_POST_UP}'":
            self.nat_rule = True
            return _ok()
        if inner == f"awg show {INTERFACE_NAME} public-key":
            private_key = interface_values(self.files[CONFIG_FILE])["PrivateKey"]
            return _ok(public_key_from_private(private_key) + "\n")
        if inner == f"awg show {INTERFACE_NAME} listen-port":
            return _ok(self._port() + "\n")
        if inner == f"awg show {INTERFACE_NAME} latest-handshakes":
            return _ok("".join(f"{key}\t{self.handshakes.get(key, 0)}\n" for key in sorted(self.interface_peers)))
        if inner.startswith("sh -c 'awg-quick strip ") and "awg syncconf" in inner:
            self.interface_peers = self.config_peer_keys()
            return _ok()
        if inner == f"iptables -t nat -C {NAT_RULE}":
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
