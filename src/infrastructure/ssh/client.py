#!/usr/bin/python
# -*- coding: utf-8 -*-
from dataclasses import dataclass

import asyncssh


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
    ) -> None:
        if bool(password) == bool(private_key):
            raise ValueError("Provide exactly one of: password or private_key")
        self.host = host
        self.username = username
        self.port = port
        self._password = password
        self._private_key = private_key
        self._conn: asyncssh.SSHClientConnection | None = None

    async def __aenter__(self) -> "SSHClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        auth: dict[str, object] = (
            {"password": self._password} if self._password else {"client_keys": [self._private_key]}
        )
        self._conn = await asyncssh.connect(
            host=self.host,
            port=self.port,
            username=self.username,
            known_hosts=None,  # осознанный компромисс MVP: см. долг про TOFU/fingerprint
            **auth,
        )

    async def execute(self, command: str) -> CommandResult:
        if self._conn is None:
            raise RuntimeError("SSH connection is not established")
        result = await self._conn.run(command)
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
