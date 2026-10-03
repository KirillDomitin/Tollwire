"""Фейковый Linux-сервер с AmneziaWG и SSH-клиент к нему.

FakeSSHClient повторяет интерфейс infrastructure.ssh.client.SSHClient (connect/close/execute/read_file/write_file),
а FakeServer держит состояние «сервера» и отвечает на ровно те команды, которые шлёт наш код. Незнакомая команда —
AssertionError: если провайдер начнёт слать что-то новое, тест сразу это покажет, а не тихо получит пустой ответ.
"""

import re
import shlex
from dataclasses import dataclass, field

from core.exceptions import SSHConnectionError
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
    docker_active: bool = True
    internet: bool = True
    reachable: bool = True
    nat_rule: bool = False
    port_published: bool = True
    interface_peers: set[str] = field(default_factory=set)  # ключи, применённые к живому интерфейсу
    handshakes: dict[str, int] = field(default_factory=dict)  # ключ → unix-время последнего рукопожатия
    commands: list[str] = field(default_factory=list)

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
        if match := re.fullmatch(r"test -f (\S+)", cmd):
            return _ok() if match.group(1) in self.files else _fail(code=1)
        if match := _PS_RE.fullmatch(cmd):
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
        if cmd.startswith(f"docker run -d --name {CONTAINER_NAME} "):
            self._start()
            return _ok("container-id\n")
        if cmd == f"docker start {CONTAINER_NAME}":
            self._start()
            return _ok()
        if cmd == f"docker stop {CONTAINER_NAME} && docker rm {CONTAINER_NAME}":
            self.containers.pop(CONTAINER_NAME, None)
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

    def __init__(self, server: FakeServer):
        self.server = server
        self.host = server.host
        self.connected = False

    async def __aenter__(self) -> "FakeSSHClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if not self.server.reachable:
            raise SSHConnectionError(f"Connection error: host={self.host}, port=22")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def execute(self, command: str, timeout: int | None = None) -> CommandResult:
        assert self.connected, "execute() before connect()"
        return self.server.run(command)

    async def read_file(self, remote_path: str) -> str:
        assert self.connected, "read_file() before connect()"
        if remote_path not in self.server.files:
            raise SSHConnectionError(f"File read: host={self.host}, path={remote_path}")
        return self.server.files[remote_path]

    async def write_file(self, remote_path: str, content: str) -> None:
        assert self.connected, "write_file() before connect()"
        self.server.files[remote_path] = content
