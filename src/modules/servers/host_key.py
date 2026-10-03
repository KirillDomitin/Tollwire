#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Закрепление ключа SSH-сервера (host key pinning): TOFU, ручное закрепление, сброс, несовпадение."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.exceptions import NotFoundError, SSHConnectionError, SSHHostKeyError
from infrastructure.ssh.host_keys import HostKey, parse_host_key, scan_host_key
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.servers.models.server import ServerStatus, VPNServer
from modules.servers.repository import ServerRepository
from modules.servers.schemas import HostKeyScan


def pinned_host_key(server: VPNServer) -> HostKey | None:
    if server.ssh_host_key and server.ssh_host_key_fingerprint:
        return HostKey(server.ssh_host_key, server.ssh_host_key_fingerprint)
    return None


class ServerHostKeyService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.servers = ServerRepository(session)
        self.audit = AuditService(session, actor)

    async def _get(self, server_id: UUID) -> VPNServer:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError
        return server

    # --- действия админа -------------------------------------------------------------------------------------------

    async def scan(self, server_id: UUID) -> HostKeyScan:
        """Какой ключ сервер предъявляет сейчас. Без входа: пароль и ключ клиента не отправляются."""
        server = await self._get(server_id)
        host, port, pinned = server.host, server.ssh_port, pinned_host_key(server)
        await self.session.rollback()  # соединение БД — обратно в пул до сетевого вызова (см. test_connection)
        presented = await scan_host_key(host, port, settings.ssh_connect_timeout)
        if presented is None:
            raise SSHConnectionError(f"Server {host} did not present a host key")
        return HostKeyScan(
            public_key=presented.public_key,
            fingerprint=presented.fingerprint,
            pinned_fingerprint=pinned.fingerprint if pinned else None,
            matches_pinned=(pinned.public_key == presented.public_key) if pinned else None,
        )

    async def pin(self, server_id: UUID, public_key: str) -> VPNServer:
        """Ручное закрепление (заменяет прежний ключ). Неверный формат — ValueError."""
        key = parse_host_key(public_key)
        server = await self._get(server_id)
        previous = server.ssh_host_key_fingerprint
        server.ssh_host_key, server.ssh_host_key_fingerprint = key.public_key, key.fingerprint
        self.audit.record(
            AuditAction.SERVER_HOST_KEY_PINNED,
            AuditEntity.SERVER,
            server.id,
            {"fingerprint": key.fingerprint, "previous": previous, "source": "manual"},
        )
        await self.session.commit()
        return server

    async def reset(self, server_id: UUID) -> VPNServer:
        """Забыть ключ (сервер переустановлен): следующее подключение закрепит новый (TOFU)."""
        server = await self._get(server_id)
        previous = server.ssh_host_key_fingerprint
        server.ssh_host_key = server.ssh_host_key_fingerprint = None
        self.audit.record(AuditAction.SERVER_HOST_KEY_RESET, AuditEntity.SERVER, server.id, {"previous": previous})
        await self.session.commit()
        return server

    # --- события подключения (вызывает SSH-клиент через ServerConnectionService) ---------------------------------

    async def learned(self, server_id: UUID, key: HostKey) -> None:
        """TOFU: закрепить ключ первого подключения. Если другой процесс успел закрепить ДРУГОЙ ключ —
        это несовпадение: SSHHostKeyError (и запись о нём)."""
        if await self.servers.pin_host_key_if_absent(server_id, key.public_key, key.fingerprint):
            self.audit.record(
                AuditAction.SERVER_HOST_KEY_PINNED,
                AuditEntity.SERVER,
                server_id,
                {"fingerprint": key.fingerprint, "previous": None, "source": "tofu"},
            )
            await self.session.commit()
            return
        server = await self._get(server_id)
        await self.session.refresh(server)
        pinned = pinned_host_key(server)
        if pinned is not None and pinned.public_key != key.public_key:
            error = SSHHostKeyError(server.host, pinned.fingerprint, key.fingerprint)
            await self.mismatch(server_id, error)
            raise error

    async def mismatch(self, server_id: UUID, error: SSHHostKeyError) -> None:
        """Сервер предъявил чужой ключ: аудит + статус ERROR (видно в списке серверов) до разбора админом."""
        server = await self._get(server_id)
        server.status = ServerStatus.ERROR
        self.audit.record(
            AuditAction.SERVER_HOST_KEY_MISMATCH,
            AuditEntity.SERVER,
            server_id,
            {"expected": error.expected, "presented": error.presented, "host": error.host},
        )
        await self.session.commit()
