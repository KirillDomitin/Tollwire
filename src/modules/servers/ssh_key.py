#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Ключ панели: панель генерирует свой SSH-ключ, ставит его на сервер и переходит на вход по нему.

Порядок установки (задача воркера INSTALL_SSH_KEY): записать публичный ключ по текущей учётке → войти новым
ключом отдельным соединением → только после этого заменить учётку в БД → снять прежние ключи панели. Любой сбой
до замены снимает новый ключ с сервера: учётка и authorized_keys остаются как были.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from core.exceptions import (
    AppError,
    CredentialChangedError,
    CredentialNotFound,
    JobAlreadyRunningError,
    NotFoundError,
    SSHAuthError,
    SSHError,
    SSHHostKeyNotPinnedError,
    SSHKeyVerificationError,
)
from core.security import encrypt_secret
from infrastructure.ssh.authorized_keys import AuthorizedKeys, generate_client_key, render_line
from infrastructure.ssh.client import SSHClient
from modules.audit.actor import AuditActor
from modules.jobs.models.job import Job, JobType
from modules.jobs.repository import JobRepository
from modules.jobs.service import JobService
from modules.servers.host_key import pinned_host_key
from modules.servers.models.credential import AuthType
from modules.servers.repository import CredentialRepository, ServerRepository
from modules.servers.schemas import CredentialCreate
from modules.servers.service import CredentialService, ServerConnectionService

logger = logging.getLogger("servers.ssh_key")

# Команда проверки входа новым ключом — константа, без пользовательского ввода.
_VERIFY_COMMAND = "uname -a"


def panel_key_marker(server_id: UUID) -> str:
    """Комментарий строки ключа в authorized_keys: по нему панель находит свои ключи этого сервера.
    У dev- и prod-панели разные id серверов — метки не пересекаются."""
    return f"vpn-panel:{server_id}"


class ServerSshKeyService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.servers = ServerRepository(session)
        self.credentials = CredentialRepository(session)
        self.jobs = JobRepository(session)

    async def schedule_install(self, server_id: UUID, credential: CredentialCreate | None) -> Job:
        """Поставить задачу установки ключа. credential — сначала сохранить логин и пароль как учётку.

        Ключ хоста должен быть закреплён до сохранения пароля: иначе пароль ушёл бы непроверенному серверу.
        """
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError
        await self.session.refresh(server)  # ключ хоста закрепляется в своей транзакции (TOFU) — читаем свежим
        if pinned_host_key(server) is None:
            raise SSHHostKeyNotPinnedError("Host key of this server is not pinned: scan and pin it first")
        if await self.jobs.has_unfinished_for_server(JobType.INSTALL_SSH_KEY, server_id):
            raise JobAlreadyRunningError("SSH key installation for this server is already in progress")
        if credential is not None:
            await CredentialService(self.session, self.actor).set_credential(server_id, credential)
        if not await self.credentials.get_by_server_id(server_id):
            raise CredentialNotFound("SSH credentials are not set for this server")
        return await JobService(self.session, self.actor).create(JobType.INSTALL_SSH_KEY, server_id=server_id)

    async def install(self, server_id: UUID) -> dict[str, Any]:
        """Установить (или заменить) ключ панели. Результат — для Job.result и аудита, без секретов."""
        connection = ServerConnectionService(self.session)
        current = await connection.create_ssh_client(server_id)
        credential = await CredentialService(self.session, self.actor).get(server_id)
        seen_at = credential.updated_at
        username = credential.username
        replaced = credential.auth_type.value

        marker = panel_key_marker(server_id)
        private, public, fingerprint = generate_client_key(marker)
        blob = public.split()[1]
        verifier = await connection.create_ssh_client(server_id, private_key=private)
        # Всё прочитано — соединение БД в пул до сетевых вызовов (SSH с повторами идёт десятки секунд). commit, а
        # не rollback: сессия общая с воркером, rollback «истёк» бы его Job (expire_on_commit=False бережёт его).
        await self.session.commit()

        async with current:
            keys = AuthorizedKeys(current)
            try:
                await keys.install(render_line(public, marker), marker)
                await self._verify(verifier)
                await self._save(server_id, seen_at, private, public, fingerprint)
            except BaseException as exc:  # и остановка воркера (CancelledError): ключ не должен остаться
                saved = await self._saved(server_id, fingerprint)
                if saved and isinstance(exc, Exception):
                    # commit дошёл, потерялся только ответ БД: учётка уже на новом ключе — снять его значило бы
                    # потерять доступ к серверу. Установка состоялась.
                    logger.warning("panel ssh key saved despite error", extra={"error": type(exc).__name__})
                else:
                    # Неизвестно (БД недоступна) — ключ оставляем: без записи в БД он безвреден, а снять ключ,
                    # на который уже перешла учётка, — потерять доступ.
                    rolled_back = await self._withdraw(keys, blob) if saved is False else False
                    if isinstance(exc, AppError):
                        exc.job_result = {"rolled_back": rolled_back}
                    raise
            removed_old = await self._prune(keys, marker, blob)

        logger.info("panel ssh key installed", extra={"server_id": str(server_id), "fingerprint": fingerprint})
        return {"fingerprint": fingerprint, "username": username, "replaced": replaced, "removed_old": removed_old}

    @staticmethod
    async def _verify(verifier: SSHClient) -> None:
        try:
            async with verifier:
                await verifier.execute(_VERIFY_COMMAND)
        except SSHAuthError as exc:
            raise SSHKeyVerificationError(
                "Server did not accept the panel key: check PubkeyAuthentication, AuthorizedKeysFile "
                "and permissions of the home directory"
            ) from exc

    async def _save(self, server_id: UUID, seen_at: datetime, private: str, public: str, fingerprint: str) -> None:
        credential = await self.credentials.get_for_update(server_id)
        if credential is None or credential.updated_at != seen_at:
            # Админ сменил или удалил учётку, пока шла задача: его выбор важнее, наш ключ не сохраняем.
            await self.session.rollback()
            raise CredentialChangedError("SSH credentials were changed while the key was being installed")
        credential.auth_type = AuthType.SSH_KEY
        credential.secret_encrypted = encrypt_secret(private)
        credential.managed_key = True
        credential.public_key = public
        credential.key_fingerprint = fingerprint
        await self.session.commit()

    @staticmethod
    async def _saved(server_id: UUID, fingerprint: str) -> bool | None:
        """Сохранён ли новый ключ в учётке (свежим чтением в своей сессии). None — БД не ответила."""
        try:
            async with session_factory() as session:
                credential = await CredentialRepository(session).get_by_server_id(server_id)
        except Exception:
            logger.warning("cannot check whether panel ssh key was saved", exc_info=True)
            return None
        return credential is not None and credential.managed_key and credential.key_fingerprint == fingerprint

    @staticmethod
    async def _withdraw(keys: AuthorizedKeys, blob: str) -> bool:
        """Снять новый ключ после сбоя. False — снять не удалось: на сервере остался ключ, приватной части
        которого нет нигде (безвреден, следующая установка уберёт его по метке)."""
        try:
            await keys.remove(blob)
        except (SSHError, OSError):
            logger.warning("panel ssh key left on server after failure", exc_info=True)
            return False
        return True

    @staticmethod
    async def _prune(keys: AuthorizedKeys, marker: str, keep_blob: str) -> int:
        # Учётка уже на новом ключе: сбой здесь не отменяет установку, прежний ключ уберёт следующая.
        try:
            return await keys.prune(marker, keep_blob)
        except SSHError:
            logger.warning("old panel ssh keys left on server", exc_info=True)
            return 0
