#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Ключи SSH-серверов (host keys): разбор, отпечаток, получение ключа без входа на сервер.

Публичный ключ сервера — не секрет: его видит любой, кто подключается к порту 22.
"""

import asyncio
from dataclasses import dataclass

import asyncssh

from core.exceptions import SSHConnectionError, SSHTimeoutError


@dataclass(frozen=True)
class HostKey:
    public_key: str  # «ssh-ed25519 AAAA…» — без комментария
    fingerprint: str  # «SHA256:…» — как показывает ssh-keygen -lf

    @classmethod
    def from_ssh_key(cls, key: asyncssh.SSHKey) -> "HostKey":
        algorithm, data = key.export_public_key().decode().split()[:2]  # без комментария (user@host)
        return cls(public_key=f"{algorithm} {data}", fingerprint=key.get_fingerprint("sha256"))

    def to_ssh_key(self) -> asyncssh.SSHKey:
        return asyncssh.import_public_key(self.public_key)


def parse_host_key(text: str) -> HostKey:
    """Строка из /etc/ssh/ssh_host_*_key.pub (комментарий допускается). Мусор — ValueError."""
    try:
        return HostKey.from_ssh_key(asyncssh.import_public_key(text.strip()))
    except (asyncssh.KeyImportError, ValueError, IndexError):
        raise ValueError("Not a valid SSH public key") from None


async def scan_host_key(host: str, port: int, timeout: float) -> HostKey | None:
    """Ключ, который сервер предъявляет при рукопожатии. Без входа: ни пароль, ни ключ клиента не отправляются."""
    try:
        key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port, config=None), timeout=timeout)
    except TimeoutError as exc:
        raise SSHTimeoutError("Host key scan timeout exceeded") from exc
    except (asyncssh.Error, OSError) as exc:
        raise SSHConnectionError(f"Host key scan failed: host={host}, port={port}") from exc
    return HostKey.from_ssh_key(key) if key else None
