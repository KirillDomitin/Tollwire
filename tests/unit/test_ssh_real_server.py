"""SSHClient против НАСТОЯЩЕГО SSH-сервера (asyncssh в этом же процессе, 127.0.0.1, случайный порт).

Подмена asyncssh.connect проверяет только аргументы; здесь — реальное рукопожатие. Главное, что доказываем:
при незакреплённом или чужом ключе сервер не получает НИ ОДНОЙ попытки входа по паролю.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import asyncssh
import pytest

from core.exceptions import SSHHostKeyError, SSHHostKeyNotPinnedError
from infrastructure.ssh.client import SSHClient
from infrastructure.ssh.host_keys import HostKey

PASSWORD = "correct-horse-battery"


@dataclass
class FakeSSHD:
    port: int
    host_key: HostKey
    client_key: str  # приватный ключ клиента, которому разрешён вход
    client_fingerprint: str
    attempts: list[str] = field(default_factory=list)  # какие попытки входа ДОШЛИ до сервера
    clients: list[SSHClient] = field(default_factory=list)


@pytest.fixture
async def sshd() -> AsyncIterator[FakeSSHD]:
    host_private = asyncssh.generate_private_key("ssh-ed25519")
    client_private = asyncssh.generate_private_key("ssh-ed25519")
    attempts: list[str] = []

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True  # аутентификация обязательна

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            attempts.append(f"password:{password}")
            return password == PASSWORD

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            attempts.append(f"public_key:{key.get_fingerprint()}")
            return key == client_private.convert_to_public()

    def run(process: asyncssh.SSHServerProcess[str]) -> None:
        process.stdout.write("Linux fake-sshd\n")
        process.exit(0)

    server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[host_private], process_factory=run)
    port = server.sockets[0].getsockname()[1]
    sshd = FakeSSHD(
        port=port,
        host_key=HostKey.from_ssh_key(host_private.convert_to_public()),
        client_key=client_private.export_private_key().decode(),
        client_fingerprint=client_private.convert_to_public().get_fingerprint(),
        attempts=attempts,
    )
    yield sshd
    # Упавший тест мог оставить соединение открытым — закрываем сами, иначе wait_closed ждал бы вечно
    # и повесил весь прогон вместо одного красного теста.
    for ssh in sshd.clients:
        await ssh.close()
    server.close()
    await asyncio.wait_for(server.wait_closed(), timeout=5)


def client(sshd: FakeSSHD, **kwargs: object) -> SSHClient:
    ssh = SSHClient("127.0.0.1", port=sshd.port, retries=1, connect_timeout=5, **kwargs)  # type: ignore[arg-type]
    sshd.clients.append(ssh)
    return ssh


async def test_key_login_with_tofu_learns_real_host_key(sshd: FakeSSHD) -> None:
    learned: list[HostKey] = []

    async def on_learned(key: HostKey) -> None:
        learned.append(key)

    async with client(sshd, private_key=sshd.client_key, on_host_key_learned=on_learned) as ssh:
        result = await ssh.execute("uname -a")
    assert result.stdout.strip() == "Linux fake-sshd"
    assert learned == [sshd.host_key]  # запомнен именно ключ этого сервера
    # Вход по ключу из БД работает (раньше ключ читался как путь к файлу). Сервер спрашивает про ключ
    # несколько раз (проверка «подходит ли» и подпись), но это всегда ТОЛЬКО наш ключ: ключи из ~/.ssh
    # и ssh-agent машины, где работает панель, серверу не предлагаются.
    assert set(sshd.attempts) == {f"public_key:{sshd.client_fingerprint}"}


async def test_password_login_with_pinned_key(sshd: FakeSSHD) -> None:
    async with client(sshd, password=PASSWORD, host_key=sshd.host_key) as ssh:
        assert (await ssh.execute("uname -a")).exit_status == 0
    assert sshd.attempts == [f"password:{PASSWORD}"]


async def test_password_never_reaches_unpinned_server(sshd: FakeSSHD) -> None:
    with pytest.raises(SSHHostKeyNotPinnedError):
        await client(sshd, password=PASSWORD).connect()
    assert sshd.attempts == []


async def test_password_never_reaches_impostor(sshd: FakeSSHD) -> None:
    """Сервер предъявил не тот ключ (подмена): обрыв в рукопожатии, до аутентификации."""
    pinned = HostKey.from_ssh_key(asyncssh.generate_private_key("ssh-ed25519").convert_to_public())
    with pytest.raises(SSHHostKeyError) as exc:
        await client(sshd, password=PASSWORD, host_key=pinned).connect()
    assert sshd.attempts == []  # пароль самозванцу не ушёл
    assert (exc.value.expected, exc.value.presented) == (pinned.fingerprint, sshd.host_key.fingerprint)
