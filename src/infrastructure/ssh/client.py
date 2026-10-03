#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import contextlib
import logging
import posixpath
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncssh

from core.config import settings
from core.exceptions import (
    SSHAuthError,
    SSHConnectionError,
    SSHHostKeyError,
    SSHHostKeyNotPinnedError,
    SSHTimeoutError,
)
from infrastructure.ssh.host_keys import HostKey, scan_host_key

# Что логируем: подключение, сбои, текст команды/путь файла (DEBUG), код возврата, длительность.
# Что НЕ логируем никогда: stdout/stderr команд и содержимое файлов (там конфиги и ключи), пароль/ключ SSH.
logger = logging.getLogger("ssh")


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


def load_private_key(text: str) -> asyncssh.SSHKey:
    """Приватный SSH-ключ из текста (PEM / OpenSSH). Ключи с паролем (passphrase) не поддерживаются.

    Строку нельзя передавать в asyncssh как есть: client_keys=[str] он понимает как ПУТЬ к файлу.
    """
    try:
        return asyncssh.import_private_key(text.strip())
    except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError, ValueError):
        raise ValueError("Not a valid unencrypted SSH private key") from None


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
        host_key: HostKey | None = None,
        on_host_key_learned: Callable[[HostKey], Awaitable[None]] | None = None,
        on_host_key_mismatch: Callable[[SSHHostKeyError], Awaitable[None]] | None = None,
    ) -> None:
        """host_key — закреплённый ключ сервера: подключение пройдёт, только если сервер докажет владение им.
        None — TOFU: ключ принимается и передаётся в on_host_key_learned (вызывающий его закрепляет)."""
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
        self.host_key = host_key
        self._on_host_key_learned = on_host_key_learned
        self._on_host_key_mismatch = on_host_key_mismatch
        self._conn: asyncssh.SSHClientConnection | None = None

    async def __aenter__(self) -> "SSHClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._password and self.host_key is None:
            # Пароль уходит серверу в открытом (внутри SSH) виде: подменённый сервер его получит. Поэтому по
            # паролю — только на сервер с закреплённым ключом (scan → сверить отпечаток → PUT host-key).
            # По ключу TOFU допустим: приватный ключ клиента серверу не передаётся.
            logger.warning("ssh password login refused: host key not pinned", extra=self._target())
            raise SSHHostKeyNotPinnedError(
                f"Host key of {self.host} is not pinned: scan and pin it before connecting with a password"
            )
        delay = self.retry_delay
        for attempt in range(1, self.retries + 1):
            try:
                await self._connect_once()
                return
            except (SSHTimeoutError, SSHConnectionError) as exc:
                if attempt == self.retries:
                    logger.warning(
                        "ssh connect failed", extra={**self._target(), "attempts": attempt, "error": str(exc)}
                    )
                    raise
                logger.info("ssh connect retry", extra={**self._target(), "attempt": attempt, "error": str(exc)})
                await asyncio.sleep(delay)
                delay *= 2

    async def _connect_once(self) -> None:
        if self._conn is not None:
            return
        # Только учётка из БД. По умолчанию asyncssh ещё и сам предлагает серверу ключи из ~/.ssh и ssh-agent
        # (раскрывая чужие ключи каждому VPN-серверу и рискуя войти не той учёткой) и читает ~/.ssh/config
        # (может незаметно подменить хост, порт, пользователя). Всё это выключено явно.
        if self._password:
            auth: dict[str, object] = {"password": self._password, "client_keys": None}
        else:
            try:
                key = load_private_key(self._private_key or "")
                # agent_path=None ssh-agent НЕ выключает (asyncssh превращает None в "", а на Windows "" —
                # агент по умолчанию). agent_identities — фильтр ключей агента: оставляем только наш, и агент
                # не предложит серверу ничего чужого (аналог IdentitiesOnly=yes в OpenSSH).
                auth = {"client_keys": [key], "agent_identities": [key.convert_to_public()]}
            except ValueError as exc:
                raise SSHAuthError("Stored SSH private key is invalid") from exc
        # Ключ проверяется ВНУТРИ рукопожатия, до отправки пароля: сервер-подмена пароль не получит.
        # (доверенные ключи, ключи CA, отозванные) — asyncssh сам сузит алгоритмы до типа закреплённого ключа.
        known_hosts: tuple[list[asyncssh.SSHKey], list[asyncssh.SSHKey], list[asyncssh.SSHKey]] | None = (
            ([self.host_key.to_ssh_key()], [], []) if self.host_key else None
        )
        try:
            self._conn = await asyncssh.connect(
                host=self.host,
                port=self.port,
                username=self.username,
                connect_timeout=self.connect_timeout,
                known_hosts=known_hosts,
                agent_path=None,
                config=None,
                **auth,
            )
            logger.info("ssh connected", extra=self._target())
        except asyncssh.HostKeyNotVerifiable as exc:
            raise await self._host_key_mismatch() from exc
        except asyncssh.PermissionDenied as exc:
            logger.warning("ssh auth failed", extra=self._target())
            raise SSHAuthError("Incorrect username or password/secret_key") from exc
        except TimeoutError as exc:
            raise SSHTimeoutError("Connection timeout exceeded") from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHConnectionError(f"Connection error: host={self.host}, port={self.port}") from exc

        if self.host_key is None:
            await self._learn_host_key(self._conn)

    async def _learn_host_key(self, conn: asyncssh.SSHClientConnection) -> None:
        server_key = conn.get_server_host_key()
        if server_key is None:
            return
        learned = HostKey.from_ssh_key(server_key)
        logger.info(
            "ssh host key learned (trust on first use)", extra={"host": self.host, "fingerprint": learned.fingerprint}
        )
        if self._on_host_key_learned:
            try:
                await self._on_host_key_learned(learned)
            except SSHHostKeyError:
                # Пока мы подключались, другой процесс закрепил ДРУГОЙ ключ — не работаем с этим сервером.
                await self.close()
                raise
        self.host_key = learned

    async def _host_key_mismatch(self) -> SSHHostKeyError:
        assert self.host_key is not None
        try:  # какой ключ предъявлен — для отчёта; получаем без входа, пароль не отправляется
            presented = await scan_host_key(self.host, self.port, self.connect_timeout)
        except (SSHTimeoutError, SSHConnectionError):
            presented = None
        error = SSHHostKeyError(self.host, self.host_key.fingerprint, presented.fingerprint if presented else None)
        logger.warning(
            "ssh host key mismatch",
            extra={"host": self.host, "expected": error.expected, "presented": error.presented},
        )
        if self._on_host_key_mismatch:
            await self._on_host_key_mismatch(error)
        return error

    async def execute(self, command: str, timeout: int | None = None) -> CommandResult:
        effective = timeout if timeout is not None else self.command_timeout
        if self._conn is None:
            raise RuntimeError("SSH connection is not established")
        started = time.perf_counter()
        try:
            result = await self._conn.run(command, timeout=effective)
        except TimeoutError as exc:
            logger.warning("ssh command timeout", extra={"host": self.host, "command": command, "timeout": effective})
            raise SSHTimeoutError("Command execution timeout exceeded") from exc
        except (asyncssh.Error, OSError) as exc:
            logger.warning("ssh command error", extra={"host": self.host, "command": command, "error": str(exc)})
            raise SSHConnectionError(f"Command execution: host={self.host}, port={self.port}") from exc

        logger.debug(
            "ssh command",
            extra={
                "host": self.host,
                "command": command,
                "exit_status": result.exit_status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return CommandResult(
            stdout=_to_str(result.stdout),
            stderr=_to_str(result.stderr),
            exit_status=result.exit_status if result.exit_status is not None else -1,
        )

    def _target(self) -> dict[str, object]:
        return {"host": self.host, "port": self.port, "username": self.username}

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
        self._conn = None

    async def write_file(self, remote_path: str, content: str) -> None:
        if self._conn is None:
            raise RuntimeError("SSH connection is not established")
        logger.debug("ssh write file", extra={"host": self.host, "remote_path": remote_path})
        # Атомарно: пишем во временный файл рядом (права 0600 — сразу при создании, а не chmod после: в конфиге
        # приватный ключ сервера), затем подменяем целиком. Обрыв посреди записи оставляет старый файл целым,
        # а не обрезанный awg0.conf без половины peer'ов.
        tmp_path = f"{remote_path}.tmp-{secrets.token_hex(4)}"
        try:
            async with self._conn.start_sftp_client() as sftp:
                await sftp.makedirs(posixpath.dirname(remote_path), exist_ok=True)
                try:
                    async with sftp.open(tmp_path, "w", asyncssh.SFTPAttrs(permissions=0o600)) as f:
                        await f.write(content)
                    await sftp.posix_rename(tmp_path, remote_path)  # rename(2): замена целиком, атомарно
                except BaseException:
                    with contextlib.suppress(asyncssh.Error, OSError):
                        await sftp.remove(tmp_path)
                    raise
        except TimeoutError as exc:
            raise SSHTimeoutError("File write timeout exceeded") from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHConnectionError(f"File write: host={self.host}, path={remote_path}") from exc

    async def read_file(self, remote_path: str) -> str:
        if self._conn is None:
            raise RuntimeError("SSH connection is not established")
        logger.debug("ssh read file", extra={"host": self.host, "remote_path": remote_path})
        try:
            async with self._conn.start_sftp_client() as sftp:
                async with sftp.open(remote_path, "r") as f:
                    return _to_str(await f.read())
        except TimeoutError as exc:
            raise SSHTimeoutError("File read timeout exceeded") from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHConnectionError(f"File read: host={self.host}, path={remote_path}") from exc
