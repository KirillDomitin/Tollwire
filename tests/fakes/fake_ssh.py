"""Фейковый Linux-сервер с AmneziaWG и SSH-клиент к нему.

FakeSSHClient повторяет интерфейс infrastructure.ssh.client.SSHClient (connect/close/execute/read_file/write_file),
а FakeServer держит состояние «сервера» и отвечает на ровно те команды, которые шлёт наш код. Незнакомая команда —
AssertionError: если провайдер начнёт слать что-то новое, тест сразу это покажет, а не тихо получит пустой ответ.
"""

import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import asyncssh

from core.exceptions import SSHAuthError, SSHConnectionError
from infrastructure.monitoring.host_metrics import HOST_METRICS_COMMAND
from infrastructure.ssh.client import CommandResult
from infrastructure.vpn.awg_config import interface_values, list_peers, public_key_from_private
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
_PS_RE = re.compile(r"docker ps -a --filter (\S+) --format '\{\{\.(\w+)\}\}'")
_EXEC = f"docker exec {CONTAINER_NAME} "


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
        if match := re.fullmatch(r"grep 'ListenPort' (\S+) \| cut -d' ' -f3", cmd):
            # Как настоящий пайп: код возврата — от cut (0), даже если файла нет и вывод пустой.
            content = self.files.get(match.group(1), "")
            port = next((line.split()[2] for line in content.splitlines() if line.startswith("ListenPort")), "")
            return _ok(port + "\n" if port else "")
        if cmd == f"docker inspect -f '{{{{.State.Running}}}}' {CONTAINER_NAME}":
            if CONTAINER_NAME not in self.containers:
                return _fail("No such object", 1)
            return _ok("true\n" if self.running else "false\n")
        if cmd == f"docker inspect -f '{{{{.HostConfig.RestartPolicy.Name}}}}' {CONTAINER_NAME}":
            if CONTAINER_NAME not in self.containers:
                return _fail("No such object", 1)
            return _ok(f"{self.restart_policy}\n")
        if cmd == f"docker update --restart unless-stopped {CONTAINER_NAME}":
            if CONTAINER_NAME not in self.containers:
                return _fail("No such container", 1)
            self.restart_policy = "unless-stopped"
            return _ok(f"{CONTAINER_NAME}\n")
        if cmd == INSPECT_MOUNTS:
            if CONTAINER_NAME not in self.containers:
                return _fail("No such object", 1)
            # Формат шаблона `{{.Source}} ` на каждое монтирование: источники через пробел, с хвостовым пробелом.
            return _ok("".join(f"{source} " for source in self.mounts.get(CONTAINER_NAME, [HOST_CONFIG_DIR])) + "\n")
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
        if cmd.startswith(_EXEC):
            if not self.running:
                return _fail(f"container {CONTAINER_NAME} is not running", 1)
            return self._exec(cmd.removeprefix(_EXEC))
        raise AssertionError(f"FakeServer: unexpected command: {command!r}")

    def _docker_ps(self, filter_arg: str, field_name: str) -> CommandResult:
        # Настоящая семантика фильтра docker: name=<регулярное выражение> ищется в имени с ведущим «/».
        key, _, pattern = shlex.split(filter_arg)[0].partition("=")
        assert key == "name", filter_arg
        lines = []
        for name, state in self.containers.items():
            if re.search(pattern, f"/{name}"):
                value = {
                    "State": state,
                    "Status": "Up 5 minutes" if state == "running" else "Exited (0) 1 minute ago",
                    "Image": "amneziavpn/amneziawg-go:latest",
                }[field_name]
                lines.append(value)
        return _ok("".join(f"{line}\n" for line in lines))

    def _exec(self, inner: str) -> CommandResult:
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
        self.server.writes.append(remote_path)
        self.server.files[remote_path] = content

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
