"""Установка ключа панели против НАСТОЯЩЕГО SSH-сервера с SFTP (asyncssh в этом же процессе).

Сервер пускает по паролю и по ключам из authorized_keys своего «домашнего каталога» (tmp_path/home/root) —
как настоящий sshd. Так проверяем не вызовы, а результат: права, отказ на симлинках и реальный вход новым ключом.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh
import pytest

from core.exceptions import AppError, SSHAuthError
from infrastructure.ssh.authorized_keys import AuthorizedKeys, generate_client_key, render_line
from infrastructure.ssh.client import SSHClient
from infrastructure.ssh.host_keys import HostKey

PASSWORD = "correct-horse-battery"
HOME = "/home/root"
MARKER = "vpn-panel:11111111-1111-4111-8111-111111111111"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="права POSIX; на Windows их нет")


@dataclass
class HomeSSHD:
    port: int
    host_key: HostKey
    root: Path  # корень SFTP-сервера
    clients: list[SSHClient] = field(default_factory=list)

    @property
    def home(self) -> Path:
        return self.root / "home" / "root"

    @property
    def ssh_dir(self) -> Path:
        return self.home / ".ssh"

    @property
    def authorized_keys(self) -> Path:
        return self.ssh_dir / "authorized_keys"


def _blobs(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.split()[1] for line in path.read_text().splitlines() if len(line.split()) >= 2}


@pytest.fixture
async def sshd(tmp_path: Path) -> AsyncIterator[HomeSSHD]:
    host_private = asyncssh.generate_private_key("ssh-ed25519")
    (tmp_path / "home" / "root").mkdir(parents=True)
    state = HomeSSHD(port=0, host_key=HostKey.from_ssh_key(host_private.convert_to_public()), root=tmp_path)

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return password == PASSWORD

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            blob = key.export_public_key("openssh").decode().split()[1]
            return blob in _blobs(state.authorized_keys)  # читаем файл при каждой попытке, как sshd

    class SFTP(asyncssh.SFTPServer):
        def __init__(self, chan: asyncssh.SSHServerChannel[bytes]):
            super().__init__(chan, chroot=str(tmp_path).encode())

        def realpath(self, path: bytes) -> bytes:
            # Сессия SFTP начинается в домашнем каталоге пользователя: realpath(".") — его адрес.
            return super().realpath(HOME.encode() if path in (b".", b"") else path)

    def run(process: asyncssh.SSHServerProcess[str]) -> None:
        process.stdout.write("Linux fake-sshd\n")
        process.exit(0)

    server = await asyncssh.create_server(
        Server, "127.0.0.1", 0, server_host_keys=[host_private], process_factory=run, sftp_factory=SFTP
    )
    state.port = server.sockets[0].getsockname()[1]
    yield state
    for ssh in state.clients:
        await ssh.close()
    server.close()
    await asyncio.wait_for(server.wait_closed(), timeout=5)


def password_client(sshd: HomeSSHD) -> SSHClient:
    ssh = SSHClient(
        "127.0.0.1", port=sshd.port, password=PASSWORD, host_key=sshd.host_key, retries=1, connect_timeout=5
    )
    sshd.clients.append(ssh)
    return ssh


def key_client(sshd: HomeSSHD, private_key: str) -> SSHClient:
    ssh = SSHClient(
        "127.0.0.1", port=sshd.port, private_key=private_key, host_key=sshd.host_key, retries=1, connect_timeout=5
    )
    sshd.clients.append(ssh)
    return ssh


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")


# --- примитивы SFTP ---------------------------------------------------------------------------------------------------


async def test_home_dir_is_users_home(sshd: HomeSSHD) -> None:
    async with password_client(sshd) as ssh:
        assert await ssh.home_dir() == HOME


async def test_read_file_optional_returns_none_for_missing_file_and_text_for_existing(sshd: HomeSSHD) -> None:
    (sshd.home / "note.txt").write_bytes(b"hello\n")  # write_text на Windows записал бы CRLF
    async with password_client(sshd) as ssh:
        assert await ssh.read_file_optional(f"{HOME}/note.txt") == "hello\n"
        assert await ssh.read_file_optional(f"{HOME}/absent.txt") is None


async def test_read_file_optional_does_not_swallow_other_errors(sshd: HomeSSHD) -> None:
    """Каталог вместо файла — не «файла нет»: молча вернуть None значило бы затереть то, чего мы не прочли."""
    async with password_client(sshd) as ssh:
        with pytest.raises(AppError):
            await ssh.read_file_optional(HOME)


@POSIX_ONLY
async def test_ensure_private_dir_creates_with_0700(sshd: HomeSSHD) -> None:
    async with password_client(sshd) as ssh:
        await ssh.ensure_private_dir(f"{HOME}/.ssh")
    assert sshd.ssh_dir.is_dir() and sshd.ssh_dir.stat().st_mode & 0o777 == 0o700


@POSIX_ONLY
async def test_ensure_private_dir_tightens_existing_loose_dir(sshd: HomeSSHD) -> None:
    sshd.ssh_dir.mkdir()
    sshd.ssh_dir.chmod(0o755)
    async with password_client(sshd) as ssh:
        await ssh.ensure_private_dir(f"{HOME}/.ssh")
    assert sshd.ssh_dir.stat().st_mode & 0o777 == 0o700


async def test_ensure_private_dir_refuses_symlink(sshd: HomeSSHD) -> None:
    elsewhere = sshd.root / "elsewhere"
    elsewhere.mkdir()
    symlink_or_skip(sshd.ssh_dir, elsewhere)
    async with password_client(sshd) as ssh:
        with pytest.raises(AppError):
            await ssh.ensure_private_dir(f"{HOME}/.ssh")
    assert list(elsewhere.iterdir()) == []  # по ссылке ничего не создано


# --- AuthorizedKeys ---------------------------------------------------------------------------------------------------


async def test_install_writes_key_and_new_key_really_logs_in(sshd: HomeSSHD) -> None:
    private, public, _ = generate_client_key(MARKER)
    line = render_line(public, MARKER)
    async with password_client(sshd) as ssh:
        assert await AuthorizedKeys(ssh).install(line, MARKER) is True
    assert sshd.authorized_keys.read_text() == line + "\n"
    async with key_client(sshd, private) as ssh:  # настоящий вход новым ключом
        assert (await ssh.execute("uname -a")).stdout.strip() == "Linux fake-sshd"


@POSIX_ONLY
async def test_install_sets_private_permissions(sshd: HomeSSHD) -> None:
    _, public, _ = generate_client_key(MARKER)
    async with password_client(sshd) as ssh:
        await AuthorizedKeys(ssh).install(render_line(public, MARKER), MARKER)
    assert sshd.ssh_dir.stat().st_mode & 0o777 == 0o700
    assert sshd.authorized_keys.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in sshd.ssh_dir.iterdir()] == ["authorized_keys"]  # временных файлов не осталось


async def test_install_is_idempotent(sshd: HomeSSHD) -> None:
    _, public, _ = generate_client_key(MARKER)
    line = render_line(public, MARKER)
    async with password_client(sshd) as ssh:
        keys = AuthorizedKeys(ssh)
        assert await keys.install(line, MARKER) is True
        assert await keys.install(line, MARKER) is False
    assert sshd.authorized_keys.read_text() == line + "\n"


async def test_install_keeps_admins_own_keys(sshd: HomeSSHD) -> None:
    sshd.ssh_dir.mkdir()
    original = "# мой ноутбук\r\nssh-rsa AAAAforeign admin@laptop\r\n"
    sshd.authorized_keys.write_bytes(original.encode())
    _, public, _ = generate_client_key(MARKER)
    line = render_line(public, MARKER)
    async with password_client(sshd) as ssh:
        await AuthorizedKeys(ssh).install(line, MARKER)
    assert sshd.authorized_keys.read_bytes().decode() == f"{original}{line}\r\n"


async def test_install_refuses_symlinked_authorized_keys(sshd: HomeSSHD) -> None:
    sshd.ssh_dir.mkdir()
    victim = sshd.root / "victim.txt"
    victim.write_text("do not touch\n")
    symlink_or_skip(sshd.authorized_keys, victim)
    _, public, _ = generate_client_key(MARKER)
    async with password_client(sshd) as ssh:
        with pytest.raises(AppError):
            await AuthorizedKeys(ssh).install(render_line(public, MARKER), MARKER)
    assert victim.read_text() == "do not touch\n"
    assert sshd.authorized_keys.is_symlink()  # ссылку не заменили файлом


async def test_remove_takes_key_away_and_login_with_it_stops_working(sshd: HomeSSHD) -> None:
    private, public, _ = generate_client_key(MARKER)
    line = render_line(public, MARKER)
    blob = public.split()[1]
    sshd.ssh_dir.mkdir()
    sshd.authorized_keys.write_text("ssh-rsa AAAAforeign admin\n")
    async with password_client(sshd) as ssh:
        keys = AuthorizedKeys(ssh)
        await keys.install(line, MARKER)
        assert await keys.remove(blob) is True
        assert await keys.remove(blob) is False  # повторное снятие безвредно
    assert sshd.authorized_keys.read_text() == "ssh-rsa AAAAforeign admin\n"
    with pytest.raises(SSHAuthError):
        await key_client(sshd, private).connect()


async def test_remove_without_authorized_keys_file_is_noop(sshd: HomeSSHD) -> None:
    async with password_client(sshd) as ssh:
        assert await AuthorizedKeys(ssh).remove("AAAAnothing") is False
    assert not sshd.authorized_keys.exists()  # файл ради «ничего не снять» не создаётся
