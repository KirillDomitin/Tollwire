#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import posixpath
from dataclasses import dataclass

import asyncssh

from core.config import settings
from core.exceptions import SSHAuthError, SSHConnectionError, SSHTimeoutError


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_status: int


def _to_str(value: str | bytes | None) -> str:
    """Приводит вывод команды к строке: bytes декодируем, None считаем пустым выводом."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


class SSHClient:
    def __init__(
        self,
        host: str,
        username: str = "root",
        port: int = 22,
        password: str | None = None,
        private_key: str | None = None,
        retries: int = 3,
        retry_delay: float = 1.0,
        connect_timeout: int = settings.ssh_connect_timeout,
        command_timeout: int = settings.ssh_command_timeout,
    ) -> None:
        if bool(password) == bool(private_key):
            raise ValueError("Provide exactly one of: password or private_key")
        self.host = host
        self.username = username
        self.port = port
        self._password = password
        self._private_key = private_key
        self.retries = retries
        self.retry_delay = retry_delay
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._conn: asyncssh.SSHClientConnection | None = None

    async def __aenter__(self) -> "SSHClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        delay = self.retry_delay
        for attempt in range(1, self.retries + 1):
            try:
                await self._connect_once()
                return
            except (SSHTimeoutError, SSHConnectionError):
                if attempt == self.retries:
                    raise
                await asyncio.sleep(delay)
                delay *= 2

    async def _connect_once(self) -> None:
        if self._conn is not None:
            return
        auth: dict[str, object] = (
            {"password": self._password} if self._password else {"client_keys": [self._private_key]}
        )
        try:
            self._conn = await asyncssh.connect(
                host=self.host,
                port=self.port,
                username=self.username,
                connect_timeout=self.connect_timeout,
                known_hosts=None,  # осознанный компромисс MVP: см. долг про TOFU/fingerprint
                **auth,
            )
        except asyncssh.PermissionDenied as exc:
            raise SSHAuthError("Incorrect username or password/secret_key") from exc
        except TimeoutError as exc:
            raise SSHTimeoutError("Connection timeout exceeded") from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHConnectionError(f"Connection error: host={self.host}, port={self.port}") from exc

    async def execute(self, command: str, timeout: int | None = None) -> CommandResult:
        effective = timeout if timeout is not None else self.command_timeout
        if self._conn is None:
            raise RuntimeError("SSH connection is not established")
        try:
            result = await self._conn.run(command, timeout=effective)
        except TimeoutError as exc:
            raise SSHTimeoutError("Command execution timeout exceeded") from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHConnectionError(f"Command execution: host={self.host}, port={self.port}") from exc

        return CommandResult(
            stdout=_to_str(result.stdout),
            stderr=_to_str(result.stderr),
            exit_status=result.exit_status if result.exit_status is not None else -1,
        )

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
        self._conn = None

    async def write_file(self, remote_path: str, content: str) -> None:
        if self._conn is None:
            raise RuntimeError("SSH connection is not established")
        try:
            async with self._conn.start_sftp_client() as sftp:
                await sftp.makedirs(posixpath.dirname(remote_path), exist_ok=True)
                async with sftp.open(remote_path, "w") as f:
                    await f.write(content)
                await self.execute(f"chmod 600 {remote_path}")
        except TimeoutError as exc:
            raise SSHTimeoutError("File write timeout exceeded") from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHConnectionError(f"File write: host={self.host}, path={remote_path}") from exc

    async def read_file(self, remote_path: str) -> str:
        if self._conn is None:
            raise RuntimeError("SSH connection is not established")
        try:
            async with self._conn.start_sftp_client() as sftp:
                async with sftp.open(remote_path, "r") as f:
                    return _to_str(await f.read())
        except TimeoutError as exc:
            raise SSHTimeoutError("File read timeout exceeded") from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHConnectionError(f"File read: host={self.host}, path={remote_path}") from exc
