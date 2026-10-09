#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Ключ панели в ~/.ssh/authorized_keys: правка текста файла (чистые функции) и запись на сервер через SFTP.

Каждая строка, которую пишет панель, — «<алгоритм> <blob> <метка>»: по метке панель потом находит свои ключи.
Чужие строки и комментарии не меняются никогда. Shell не используется: пути и содержимое не попадают в команды.
"""

import posixpath
import re

import asyncssh

from infrastructure.ssh.client import SSHClient

_KEY_TYPE_PREFIXES = ("ssh-", "ecdsa-", "sk-")


def render_line(public_openssh: str, marker: str) -> str:
    """Строка для authorized_keys: прежний комментарий ключа заменяется меткой панели."""
    algorithm, blob = public_openssh.split()[:2]
    return f"{algorithm} {blob} {marker}"


def _key_fields(line: str) -> tuple[str, str, str] | None:
    """(алгоритм, blob, комментарий) строки authorized_keys; None — комментарий, пустая строка или не ключ.

    Строка может начинаться с опций (`command="echo a b",no-pty ssh-rsa AAAA...`): они идут до первого пробела
    вне кавычек. Ключ ищется по позиции поля, а не подстрокой — иначе blob в чужом комментарии сошёл бы за ключ.
    """
    rest = line.strip()
    if not rest or rest.startswith("#"):
        return None
    if not rest.startswith(_KEY_TYPE_PREFIXES):
        index, quoted = 0, False
        while index < len(rest):
            char = rest[index]
            if char == "\\" and quoted:
                index += 2
                continue
            if char == '"':
                quoted = not quoted
            elif char.isspace() and not quoted:
                break
            index += 1
        rest = rest[index:].lstrip()
    parts = rest.split(None, 2)
    if len(parts) < 2:
        return None
    return parts[0], parts[1], parts[2] if len(parts) > 2 else ""


def _split(text: str) -> tuple[list[str], str]:
    """Строки файла и его перевод строки — по первой строке (CRLF сохраняется). Смешанные окончания режутся
    оба: иначе несколько строк слились бы в одну и ключи в них не нашлись бы."""
    first = text.find("\n")
    newline = "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"
    lines = re.split(r"\r?\n", text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines, newline


def _join(lines: list[str], newline: str) -> str:
    return "".join(line + newline for line in lines)


def add(text: str, line: str) -> tuple[str, bool]:
    """Дописать строку ключа, если такого ключа (того же blob) в файле ещё нет."""
    fields = _key_fields(line)
    assert fields is not None, "line must be a key line"
    lines, newline = _split(text)
    if any((existing := _key_fields(current)) and existing[1] == fields[1] for current in lines):
        return text, False
    return _join([*lines, line], newline), True


def remove_blob(text: str, blob: str) -> tuple[str, bool]:
    """Убрать строки с этим ключом (по blob)."""
    lines, newline = _split(text)
    kept = [current for current in lines if not ((fields := _key_fields(current)) and fields[1] == blob)]
    if len(kept) == len(lines):
        return text, False
    return _join(kept, newline), True


def remove_marker(text: str, marker: str, keep_blob: str | None) -> tuple[str, int]:
    """Убрать строки панели с этой меткой (комментарий ключа ровно равен метке), кроме keep_blob."""
    lines, newline = _split(text)
    kept = [
        current
        for current in lines
        if not ((fields := _key_fields(current)) and fields[2] == marker and fields[1] != keep_blob)
    ]
    removed = len(lines) - len(kept)
    if not removed:
        return text, 0
    return _join(kept, newline), removed


def generate_client_key(comment: str) -> tuple[str, str, str]:
    """Новый ключ панели ed25519: (приватный OpenSSH, публичный OpenSSH, отпечаток SHA256:...).

    Без passphrase: в БД ключ хранится зашифрованным (Fernet), как и пароль.
    """
    key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
    public = key.convert_to_public()
    return (
        key.export_private_key("openssh").decode(),
        public.export_public_key("openssh").decode().strip(),
        public.get_fingerprint(),
    )


class AuthorizedKeys:
    """authorized_keys пользователя, под которым открыто соединение. Только SFTP; симлинки — отказ."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    async def _paths(self) -> tuple[str, str]:
        ssh_dir = posixpath.join(await self.ssh.home_dir(), ".ssh")
        return ssh_dir, posixpath.join(ssh_dir, "authorized_keys")

    async def install(self, line: str, marker: str) -> bool:
        """Добавить строку ключа панели. False — такой ключ уже есть. Права: ~/.ssh 0700, файл 0600."""
        if _key_fields(line) is None or not line.endswith(f" {marker}"):
            raise ValueError("Panel key line must be a key with the panel marker")
        ssh_dir, path = await self._paths()
        await self.ssh.ensure_private_dir(ssh_dir)
        text, added = add(await self.ssh.read_file_optional(path) or "", line)
        if added:
            await self.ssh.write_file(path, text)
        return added

    async def remove(self, blob: str) -> bool:
        """Снять ключ (по blob). False — его не было; файла нет — ничего не создаём."""
        _, path = await self._paths()
        current = await self.ssh.read_file_optional(path)
        if current is None:
            return False
        text, removed = remove_blob(current, blob)
        if removed:
            await self.ssh.write_file(path, text)
        return removed

    async def prune(self, marker: str, keep_blob: str) -> int:
        """Снять прежние ключи панели с этой меткой (после ротации или сбоя), кроме keep_blob. Сколько снято."""
        _, path = await self._paths()
        current = await self.ssh.read_file_optional(path)
        if current is None:
            return 0
        text, removed = remove_marker(current, marker, keep_blob)
        if removed:
            await self.ssh.write_file(path, text)
        return removed
