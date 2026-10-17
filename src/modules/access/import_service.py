#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import (
    CredentialNotFound,
    ImportNotAllowedError,
    InvalidDataError,
    JobAlreadyRunningError,
    NotFoundError,
)
from core.security import encrypt_secret
from infrastructure.vpn.provider import ImportablePeer
from modules.access.models import AccessGrantStatus, CredentialSource
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import AccessGrantRepository, SubscriptionRepository, VPNCredentialRepository
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.jobs.models.job import Job, JobType
from modules.jobs.repository import JobRepository
from modules.jobs.service import JobService
from modules.protocols.models.protocol import VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.servers.discovery import ServerDiscoveryService
from modules.servers.repository import CredentialRepository, ServerRepository
from modules.servers.schemas import ClientImportCreate, ClientImportParams, ImportClient
from modules.servers.service import ServerConnectionService
from modules.vpn_users.models.user import VPNUser, VPNUserStatus
from modules.vpn_users.repository import VPNUserRepository

logger = logging.getLogger("access.import")

_COUNTED = ("imported", "already", "gone", "unsupported", "conflict")


class _Conflict(Exception):
    """Клиента нельзя импортировать из-за данных панели; причина попадает в отчёт задачи (без секретов)."""


class ClientImportService:
    """Перенос клиентов установленной AmneziaVPN в БД панели: пользователь, подписка, доступ и credential без
    конфига (приватный ключ остался в приложении), но с публичным ключом, адресом и PSK. Сервер не меняется."""

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.servers = ServerRepository(session)
        self.server_credentials = CredentialRepository(session)
        self.protocols = ProtocolRepository(session)
        self.users = VPNUserRepository(session)
        self.subscriptions = SubscriptionRepository(session)
        self.grants = AccessGrantRepository(session)
        self.credentials = VPNCredentialRepository(session)
        self.connections = ServerConnectionService(session)
        self.audit = AuditService(session, actor)

    async def schedule(self, server_id: UUID, data: ClientImportCreate) -> Job:
        """Ставит IMPORT_CLIENTS. Быстрые проверки по БД (SSH не открывается): сервер и учётка, пользователи из
        user_id, каждый ключ есть в ПОСЛЕДНЕМ отчёте поиска этого протокола и поддерживается, другого
        незавершённого импорта на сервере нет. Воркер всё равно перечитает сервер."""
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError("Server not found")
        if not await self.server_credentials.get_by_server_id(server_id):
            raise CredentialNotFound("SSH credentials are not set for this server")
        for client in data.clients:
            if client.user_id is not None and not await self.users.get_by_id(client.user_id):
                raise NotFoundError(f"VPN user '{client.user_id}' not found")

        report = await ServerDiscoveryService(self.session).latest(server_id)
        found = next((item for item in report["protocols"] if item["type"] == data.protocol_type.value), None)
        known = {client["public_key"]: client for client in found["clients"]} if found else {}
        for client in data.clients:
            entry = known.get(client.public_key)
            if entry is None:
                raise ImportNotAllowedError("Client is not in the last discovery report, run the discovery first")
            if not entry.get("supported", False):
                raise ImportNotAllowedError("Client has an unsupported configuration (several addresses)")

        if await JobRepository(self.session).has_unfinished_for_server(JobType.IMPORT_CLIENTS, server_id):
            raise JobAlreadyRunningError("Another import is already running on this server")
        # Ключи, имена и email - в параметры (они не секретны); PSK сюда не попадает: воркер читает его с сервера.
        params = data.model_dump(mode="json")
        return await JobService(self.session, self.actor).create(JobType.IMPORT_CLIENTS, server_id, params=params)

    async def run(self, job: Job) -> dict[str, Any]:
        """Выполняет задачу в воркере под ServerLock. Заново читает сервер (отчёт поиска мог устареть); сбой одного
        клиента не рушит остальных (вложенная транзакция). Commit делает воркер вместе со статусом задачи:
        упавшая задача не оставляет частичного импорта. Результат - без секретов."""
        try:
            params = ClientImportParams.model_validate(job.params)
        except ValidationError:
            raise InvalidDataError("Import job has invalid params") from None
        if job.server_id is None:
            raise InvalidDataError("Import job has no server")
        server_id = job.server_id
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError(f"Server '{server_id}' not found")

        ssh_client = await self.connections.create_ssh_client(server_id)
        provider = self.connections.build_provider(ssh_client, params.protocol_type)
        async with ssh_client:
            peers = {peer.public_key: peer for peer in await provider.read_importable()}
            port = (await provider.get_status()).port

        protocol = await self.protocols.get_by_server_and_type(server_id, params.protocol_type)
        if protocol is None:
            protocol = await self.protocols.create(
                VPNProtocol(server_id=server_id, type=params.protocol_type, port=port)
            )
            await self.session.flush()
        live_keys = {
            credential.client_public_key
            for credential, _, _ in await self.credentials.list_with_access_by_protocol_id(protocol.id)
            if credential.revoked_at is None
        }

        result: dict[str, Any] = {
            "protocol_id": str(protocol.id),
            "imported": [],
            "already": [],
            "gone": [],
            "unsupported": [],
            "conflict": [],
        }
        for client in params.clients:
            peer = peers.get(client.public_key)
            if peer is None:
                result["gone"].append(client.public_key)
            elif not peer.supported or peer.address is None:
                result["unsupported"].append(client.public_key)
            elif client.public_key in live_keys:
                result["already"].append(client.public_key)
            else:
                try:
                    async with self.session.begin_nested():
                        imported = await self._import_one(server_id, protocol, client, peer, params.valid_until)
                except _Conflict as exc:
                    result["conflict"].append({"public_key": client.public_key, "reason": str(exc)})
                except DBAPIError:
                    # Уникальность или данные, которые БД не приняла (в том числе гонка с API): текст ошибки наружу не
                    # отдаём - в нём параметры INSERT.
                    result["conflict"].append(
                        {"public_key": client.public_key, "reason": "Conflicts with existing data"}
                    )
                else:
                    result["imported"].append(imported)
        await self.session.flush()
        logger.info("clients imported", extra={name: len(result[name]) for name in _COUNTED})
        return result

    async def _import_one(
        self, server_id: UUID, protocol: VPNProtocol, client: ImportClient, peer: ImportablePeer, valid_until: datetime
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        user, created = await self._user(client)
        subscription = await self._subscription(user, now, valid_until)
        if await self.grants.find_active_conflict(user.id, server_id, protocol.id):
            raise _Conflict("User already has an access to this server and protocol")
        grant = await self.grants.create(
            AccessGrant(
                subscription_id=subscription.id,
                server_id=server_id,
                protocol_id=protocol.id,
                status=AccessGrantStatus.ACTIVE,
            )
        )
        credential = await self.credentials.create(
            VPNCredential(
                access_grant_id=grant.id,
                encrypted_config=None,
                client_address=str(peer.address),
                client_public_key=peer.public_key,
                source=CredentialSource.IMPORTED,
                encrypted_psk=encrypt_secret(peer.psk) if peer.psk else None,
                external_name=peer.name,
            )
        )
        # Без этой записи по журналу не восстановить, кто и когда получил доступ (Job.result со временем чистится).
        self.audit.record(
            AuditAction.CREDENTIAL_IMPORTED,
            AuditEntity.VPN_CREDENTIAL,
            credential.id,
            {
                "access_grant_id": grant.id,
                "server_id": server_id,
                "user_id": user.id,
                "public_key": peer.public_key,
            },
        )
        if created:
            # Имя и email в журнал не пишем: они из запроса админа, а происхождение видно по source.
            self.audit.record(AuditAction.VPN_USER_CREATED, AuditEntity.VPN_USER, user.id, {"source": "import"})
        return {"public_key": peer.public_key, "user_id": str(user.id), "credential_id": str(credential.id)}

    async def _user(self, client: ImportClient) -> tuple[VPNUser, bool]:
        if client.user_id is not None:
            user = await self.users.get_by_id(client.user_id)
            if user is None:
                raise _Conflict("User not found")
            return user, False
        if client.email is not None and await self.users.get_by_email(client.email):
            raise _Conflict("User with this email already exists")
        new_user = VPNUser(username=client.username, email=client.email, status=VPNUserStatus.ACTIVE)
        return await self.users.create(new_user), True

    async def _subscription(self, user: VPNUser, now: datetime, valid_until: datetime) -> Subscription:
        """Живая подписка пользователя (если она действует сейчас) или новая ACTIVE на срок из запроса. Подписка,
        которая ещё не началась, не подходит: sync счёл бы доступ неживым и снял бы peer."""
        live = await self.subscriptions.get_active_by_user_id(user.id)
        if live is None:
            if valid_until <= now:
                raise _Conflict("valid_until is already in the past")
            new_subscription = Subscription(
                user_id=user.id, status=AccessGrantStatus.ACTIVE, valid_from=now, valid_until=valid_until
            )
            return await self.subscriptions.create(new_subscription)
        if not live.valid_from <= now < live.valid_until:
            raise _Conflict("Subscription of the user is not in effect now")
        return live
