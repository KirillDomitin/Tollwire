#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Ссылки на страницу конфига: выпуск человеку в Telegram и публичное открытие по токену.

Токен виден один раз, при выпуске, в БД только SHA-256. Ссылка живёт, пока жив доступ, и это проверяется при каждом
открытии (клиент не отозван, грант и подписка в силе, срок не вышел): ни планировщик, ни хуки отзыва не нужны, а
истёкший срок и грант, отозванный до выполнения задачи REVOKE_ACCESS, закрывают ссылку сразу."""

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.exceptions import NotFoundError, ReasonConflictError
from core.security import generate_token, hash_token
from modules.access.models import CredentialSource
from modules.access.models.access_grant import AccessGrant
from modules.access.models.vpn_credential import VPNCredential
from modules.access.service import VPNCredentialService, inactive_reason, is_terminal
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.bot.models.config_link import ConfigLink
from modules.bot.repository import ConfigLinkRepository, OutboxRepository
from modules.bot.schemas import ConfigLinkRead, PublicConfigRead
from modules.jobs.models.job import JobType
from modules.jobs.repository import JobRepository
from modules.vpn_users.repository import VPNUserRepository

logger = logging.getLogger("bot")

# Причины отказа выпуска (поле reason ответа 409): по ним бот объясняет человеку, что не так.
CONFIG_UNAVAILABLE = "config_unavailable"  # клиенты подхвачены из AmneziaVPN, приватного ключа у панели нет
NOT_READY = "not_ready"  # клиента ещё выдают
NO_ACCESS = "no_access"  # доступа нет: отозван, срок вышел или его не было
NOT_CONFIGURED = "not_configured"  # публичный адрес панели не задан

TOKEN_BYTES = 32  # 43 символа base64url
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_FILE_NAME_DEFAULT = "amnezia"
_FILE_NAME_MAX = 40


def file_name_of(server_name: str) -> str:
    """Имя файла .conf из имени сервера: только ASCII [A-Za-z0-9_-], иначе имя по умолчанию."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", server_name).strip("-")[:_FILE_NAME_MAX].strip("-")
    return name or _FILE_NAME_DEFAULT


class ConfigLinkService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.links = ConfigLinkRepository(session)
        self.users = VPNUserRepository(session)
        self.audit = AuditService(session, actor)

    # --- выпуск ----------------------------------------------------------------------------------------------------

    async def issue_for_telegram(self, telegram_id: int) -> tuple[ConfigLink, str]:
        """Новая ссылка на конфиг человека; прежняя живая ссылка того же клиента отзывается. Токен - только здесь."""
        if await OutboxRepository(self.session).unmark_blocked(telegram_id):
            await self.session.commit()  # человек сам написал боту: пометка «заблокировал» снимается и при отказе
        try:
            return await self._issue(telegram_id)
        except IntegrityError:
            # Частичный уникальный индекс поймал гонку двух выпусков: повтор видит чужую ссылку и заменяет её.
            await self.session.rollback()
        try:
            return await self._issue(telegram_id)
        except IntegrityError:
            await self.session.rollback()
            raise ReasonConflictError(NO_ACCESS) from None

    async def _issue(self, telegram_id: int) -> tuple[ConfigLink, str]:
        user = await self.users.get_by_telegram_id(telegram_id)
        if user is None:
            raise NotFoundError("User not found")
        try:
            result = await self.stage_issue(user.id, source="bot")
        except ReasonConflictError:
            await self.session.rollback()
            raise
        await self.session.commit()
        return result

    async def ensure_issuable(self, user_id: UUID, grant_id: UUID | None = None) -> None:
        """Можно ли выпустить ссылку человеку сейчас - без выпуска. Отказ - ReasonConflictError с причиной."""
        if settings.public_base_url is None:
            raise ReasonConflictError(NOT_CONFIGURED)
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found")
        await self._choose_credential(user.id, user.telegram_id, grant_id)

    async def stage_issue(
        self,
        user_id: UUID,
        grant_id: UUID | None = None,
        *,
        source: str,
        meta: Mapping[str, Any] | None = None,
    ) -> tuple[ConfigLink, str]:
        """Выпуск ссылки человеку БЕЗ commit: ссылка, отзыв прежней и запись в аудит живут в транзакции вызывающего.
        grant_id - выпустить на клиента этого доступа (рассылка «по протоколу»); иначе клиент - как для бота."""
        if settings.public_base_url is None:
            logger.warning("config links are not issued: set PUBLIC_URL (or PANEL_DOMAIN) in the panel settings")
            raise ReasonConflictError(NOT_CONFIGURED)
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found")
        credential = await self._choose_credential(user.id, user.telegram_id, grant_id)

        # Клиент блокируется до конца транзакции: два одновременных выпуска идут по очереди, и живая ссылка одна.
        locked = await self.links.lock_credential(credential.id)
        if locked is None or locked.revoked_at is not None:
            raise ReasonConflictError(NO_ACCESS)
        replaced = False
        for old in await self.links.live_links(credential.id):
            old.revoked_at = datetime.now(timezone.utc)
            replaced = True
        await self.session.flush()  # отзыв раньше вставки: иначе частичный индекс отказал бы новой ссылке

        token = generate_token(TOKEN_BYTES)
        link = await self.links.create(ConfigLink(token_hash=hash_token(token), credential_id=credential.id))
        self.audit.record(
            AuditAction.CONFIG_LINK_CREATED,
            AuditEntity.CONFIG_LINK,
            link.id,
            {
                "credential_id": credential.id,
                "user_id": user.id,
                "source": source,
                **{key: value for key, value in (meta or {}).items() if value is not None},
                "replaced": replaced,
            },
        )
        return link, token

    async def _choose_credential(
        self, user_id: UUID, telegram_id: int | None, grant_id: UUID | None = None
    ) -> VPNCredential:
        """Клиент, на которого выпускается ссылка: выданный панелью (с конфигом) и с живым доступом. С grant_id -
        только клиент этого доступа."""
        now = datetime.now(timezone.utc)
        live: list[tuple[VPNCredential, AccessGrant]] = [
            (credential, grant)
            for credential, grant, subscription in await self.links.credentials_of_user(user_id)
            if inactive_reason(grant, subscription, now) is None and (grant_id is None or grant.id == grant_id)
        ]
        issued = [
            (credential, grant)
            for credential, grant in live
            if credential.source == CredentialSource.ISSUED and credential.encrypted_config is not None
        ]
        if issued:
            registered = await self.links.registered_grant_id(telegram_id) if telegram_id is not None else None
            for credential, grant in issued:
                if grant.id == registered:  # клиент, выданный по регистрации через бота, важнее более нового
                    return credential
            return max(issued, key=lambda pair: pair[0].created_at)[0]
        if live:
            raise ReasonConflictError(CONFIG_UNAVAILABLE)  # только подхваченные из AmneziaVPN
        if await self._being_issued(user_id, now, grant_id):
            raise ReasonConflictError(NOT_READY)
        raise ReasonConflictError(NO_ACCESS)

    async def _being_issued(self, user_id: UUID, now: datetime, grant_id: UUID | None = None) -> bool:
        """Есть живой доступ, клиента по которому выдаёт воркер (CREATE_USER в очереди или в работе)."""
        jobs = JobRepository(self.session)
        for grant, subscription in await self.links.grants_of_user(user_id):
            if is_terminal(grant.status) or inactive_reason(grant, subscription, now) is not None:
                continue
            if grant_id is not None and grant.id != grant_id:
                continue
            if await jobs.has_unfinished_for_grant(JobType.CREATE_USER, grant.id):
                return True
        return False

    async def url_of(self, link: ConfigLink, token: str) -> ConfigLinkRead:
        """Ответ боту: адрес страницы (токен во фрагменте: на сервер и в его логи он не попадает) и конец доступа."""
        valid_until = await self.links.valid_until_of(link.credential_id)
        base = settings.public_base_url
        if valid_until is None or base is None:  # ссылку только что выпустили: сюда попасть нельзя
            raise ReasonConflictError(NO_ACCESS)
        return ConfigLinkRead(config_url=f"{base}/c#{token}", valid_until=valid_until)

    # --- открытие --------------------------------------------------------------------------------------------------

    async def open(self, token: str) -> PublicConfigRead:
        """Данные страницы по токену. Любой отказ - NotFoundError без подробностей: из ответа нельзя понять, был ли
        такой токен, заменили его, отозвали или закончился доступ."""
        if not _TOKEN_SHAPE.fullmatch(token):
            raise NotFoundError("Not found")
        found = await self.links.open_target(hash_token(token))
        now = datetime.now(timezone.utc)
        if found is None:
            raise NotFoundError("Not found")
        link, credential, grant, subscription, username, server_name = found
        if inactive_reason(grant, subscription, now) is not None:
            raise NotFoundError("Not found")  # расшифровка - только после всех проверок

        rendered = await VPNCredentialService(self.session, self.actor).render_config(credential, grant)
        link.last_opened_at = now
        await self.session.commit()
        return PublicConfigRead(
            name=username,
            valid_until=subscription.valid_until,
            server_name=server_name,
            config=rendered.config,
            share_link=rendered.share_link,
            share_qr=rendered.share_qr,
            file_name=file_name_of(server_name),
        )
