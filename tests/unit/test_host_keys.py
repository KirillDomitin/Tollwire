"""Проверка ключа SSH-сервера в SSHClient: что передаётся в asyncssh, TOFU, несовпадение — без настоящей сети."""

from typing import Any

import asyncssh
import pytest

from core.exceptions import SSHHostKeyError
from infrastructure.ssh import client as client_module
from infrastructure.ssh.client import SSHClient
from infrastructure.ssh.host_keys import HostKey, parse_host_key


def new_key() -> HostKey:
    return HostKey.from_ssh_key(asyncssh.generate_private_key("ssh-ed25519").convert_to_public())


class FakeConn:
    def __init__(self, server_key: HostKey):
        self.server_key = server_key

    def get_server_host_key(self) -> asyncssh.SSHKey:
        return self.server_key.to_ssh_key()

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


@pytest.fixture
def connect_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Подмена asyncssh.connect: «сервер» с ключом calls[0]["server_key"], проверка known_hosts как у asyncssh."""
    server_key = new_key()
    calls: list[dict[str, Any]] = [{"server_key": server_key}]

    async def fake_connect(**kwargs: Any) -> FakeConn:
        calls.append(kwargs)
        trusted = kwargs["known_hosts"]
        if trusted is not None and server_key.to_ssh_key() not in trusted[0]:
            raise asyncssh.HostKeyNotVerifiable("Host key is not trusted")
        return FakeConn(server_key)

    monkeypatch.setattr(client_module.asyncssh, "connect", fake_connect)
    return calls


def test_parse_host_key_drops_comment_and_matches_ssh_keygen() -> None:
    key = new_key()
    parsed = parse_host_key(f"{key.public_key} root@vpn-1\n")
    assert parsed == key and parsed.fingerprint.startswith("SHA256:")
    with pytest.raises(ValueError):
        parse_host_key("ssh-ed25519 not-base64!!")


async def test_tofu_accepts_and_reports_key(connect_calls: list[dict[str, Any]]) -> None:
    server_key = connect_calls[0]["server_key"]
    learned: list[HostKey] = []

    async def on_learned(key: HostKey) -> None:
        learned.append(key)

    client = SSHClient("203.0.113.10", password="pw", on_host_key_learned=on_learned)
    await client.connect()
    assert connect_calls[1]["known_hosts"] is None  # ключа ещё нет — принимаем
    assert learned == [server_key] and client.host_key == server_key


async def test_pinned_key_is_passed_to_handshake(connect_calls: list[dict[str, Any]]) -> None:
    server_key = connect_calls[0]["server_key"]
    learned: list[HostKey] = []

    async def on_learned(key: HostKey) -> None:
        learned.append(key)

    client = SSHClient("203.0.113.10", password="pw", host_key=server_key, on_host_key_learned=on_learned)
    await client.connect()
    trusted, ca_keys, revoked = connect_calls[1]["known_hosts"]
    assert trusted == [server_key.to_ssh_key()] and ca_keys == [] and revoked == []
    assert learned == []  # закреплённый ключ не «узнаётся» заново


async def test_mismatch_is_refused_without_retries(
    connect_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    server_key = connect_calls[0]["server_key"]
    pinned = new_key()  # закреплён другой ключ — сервер «подменён»

    async def fake_scan(host: str, port: int, timeout: float) -> HostKey:
        return server_key

    monkeypatch.setattr(client_module, "scan_host_key", fake_scan)
    reported: list[SSHHostKeyError] = []

    async def on_mismatch(error: SSHHostKeyError) -> None:
        reported.append(error)

    client = SSHClient("203.0.113.10", password="pw", host_key=pinned, retries=3, on_host_key_mismatch=on_mismatch)
    with pytest.raises(SSHHostKeyError) as exc:
        await client.connect()
    assert (exc.value.expected, exc.value.presented) == (pinned.fingerprint, server_key.fingerprint)
    assert reported == [exc.value]
    assert len(connect_calls) == 2  # одна попытка: несовпадение не повторяем, как сетевой сбой


async def test_conflicting_pin_during_tofu_closes_connection(connect_calls: list[dict[str, Any]]) -> None:
    async def on_learned(key: HostKey) -> None:
        raise SSHHostKeyError("203.0.113.10", "SHA256:other", key.fingerprint)

    client = SSHClient("203.0.113.10", password="pw", on_host_key_learned=on_learned)
    with pytest.raises(SSHHostKeyError):
        await client.connect()
    assert client._conn is None  # соединение с этим сервером не оставляем
