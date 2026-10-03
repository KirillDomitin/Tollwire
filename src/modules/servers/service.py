#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.postgres import session_factory
from core.exceptions import (
    AlreadyExistsError,
    CredentialNotFound,
    NotFoundError,
    SSHError,
    SSHHostKeyError,
    UnsupportedProtocolError,
)
from core.security import decrypt_secret, encrypt_secret
from infrastructure.ssh.client import SSHClient
from infrastructure.ssh.host_keys import HostKey
from infrastructure.vpn.amnezia import AmneziaInstaller, AmneziaWGProvider
from infrastructure.vpn.provider import ProviderStatus, VPNProvider
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService, diff_changes
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.servers.host_key import ServerHostKeyService, pinned_host_key
from modules.servers.models.credential import AuthType, ServerCredential
from modules.servers.models.server import ServerStatus, VPNServer
from modules.servers.repository import CredentialRepository, ServerRepository
from modules.servers.schemas import CredentialCreate, ServerCreate, ServerUpdate, TestConnectionResult


class ServerService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.servers = ServerRepository(session)
        self.protocols = ProtocolRepository(session)
        self.audit = AuditService(session, actor)

    async def create(self, data: ServerCreate) -> VPNServer:
        if await self.servers.get_by_name(data.name):
            raise AlreadyExistsError
        new_server = VPNServer(name=data.name, host=data.host, ssh_port=data.ssh_port, description=data.description)
        server = await self.servers.create(new_server)
        self.audit.record(
            AuditAction.SERVER_CREATED,
            AuditEntity.SERVER,
            server.id,
            {"name": server.name, "host": server.host, "ssh_port": server.ssh_port},
        )
        await self.session.commit()
        return server

    async def get(self, server_id: UUID) -> VPNServer:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError
        return server

    async def list_all(self) -> Sequence[VPNServer]:
        return await self.servers.list_all()

    async def list_protocols(self, server_id: UUID) -> Sequence[VPNProtocol]:
        """Установленные протоколы сервера — отсюда админ берёт protocol_id для выдачи доступа."""
        await self.get(server_id)
        return await self.protocols.list_by_server_id(server_id)

    async def update(self, server_id: UUID, data: ServerUpdate) -> VPNServer:
        server = await self.get(server_id)
        fields = data.model_dump(exclude_unset=True)
        changes = diff_changes(server, fields)
        meta: dict[str, object] = {"changes": changes}
        # Другой адрес — другая машина: старый ключ к ней не относится. Следующее подключение закрепит новый.
        if "host" in changes and server.ssh_host_key is not None:
            meta["host_key_reset"] = {"previous": server.ssh_host_key_fingerprint}
            server.ssh_host_key = server.ssh_host_key_fingerprint = None
        for field, value in fields.items():
            setattr(server, field, value)
        if changes:
            self.audit.record(AuditAction.SERVER_UPDATED, AuditEntity.SERVER, server.id, meta)
        await self.session.commit()
        return server

    async def delete(self, server_id: UUID) -> None:
        server = await self.get(server_id)
        await self.servers.delete(server)
        self.audit.record(
            AuditAction.SERVER_DELETED, AuditEntity.SERVER, server.id, {"name": server.name, "host": server.host}
        )
        await self.session.commit()


class CredentialService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.credentials = CredentialRepository(session)
        self.servers = ServerRepository(session)
        self.audit = AuditService(session, actor)

    async def set_credential(self, server_id: UUID, data: CredentialCreate) -> ServerCredential:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError
        encrypted_secret = encrypt_secret(data.secret)
        # Секрет в журнал не попадает — только факт смены и несекретные поля.
        self.audit.record(
            AuditAction.SERVER_CREDENTIALS_SET,
            AuditEntity.SERVER,
            server_id,
            {"username": data.username, "auth_type": data.auth_type},
        )
        cred = await self.credentials.get_by_server_id(server_id)
        if cred:
            cred.username = data.username
            cred.auth_type = data.auth_type
            cred.secret_encrypted = encrypted_secret
            await self.session.commit()
            return cred

        new_cred = ServerCredential(
            server_id=server_id, auth_type=data.auth_type, username=data.username, secret_encrypted=encrypted_secret
        )
        await self.credentials.create(new_cred)
        await self.session.commit()
        return new_cred

    async def get(self, server_id: UUID) -> ServerCredential:
        cred = await self.credentials.get_by_server_id(server_id)
        if not cred:
            raise CredentialNotFound
        return cred

    async def delete(self, server_id: UUID) -> None:
        cred = await self.credentials.get_by_server_id(server_id)
        if not cred:
            raise CredentialNotFound
        await self.credentials.delete(cred)
        self.audit.record(AuditAction.SERVER_CREDENTIALS_DELETED, AuditEntity.SERVER, server_id)
        await self.session.commit()

    async def get_decrypted_secret(self, server_id: UUID) -> str:
        cred = await self.credentials.get_by_server_id(server_id)
        if not cred:
            raise CredentialNotFound
        return decrypt_secret(cred.secret_encrypted)


class ServerConnectionService:
    def __init__(self, session: AsyncSession):
        self.session = session
        # Только чтение учётки для SSH — в журнал ничего не пишется, поэтому системный актор.
        self.credentials = CredentialService(session, SYSTEM_ACTOR)
        self.servers = ServerRepository(session)
        self.protocols = ProtocolRepository(session)

    async def test_connection(self, server_id: UUID) -> TestConnectionResult:
        ssh_client = await self.create_ssh_client(server_id)
        try:
            async with ssh_client:
                result = await ssh_client.execute("uname -a")
            success = True
            message = f"Successful connection: {result.stdout}"
        except SSHError as exc:
            success = False
            message = f"ERROR: {exc}"

        return TestConnectionResult(success=success, message=message)

    def build_provider(self, ssh_client: SSHClient, protocol_type: ProtocolType) -> VPNProvider:
        match protocol_type:
            case ProtocolType.AMNEZIA_WG:
                return AmneziaWGProvider(ssh_client)
            case _:
                raise UnsupportedProtocolError(f"Unsupported protocol '{protocol_type}'")

    async def server_install(self, server_id: UUID) -> None:
        ssh_client = await self.create_ssh_client(server_id)
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError(f"Server '{server_id}' not found")

        server.status = ServerStatus.INSTALLING
        await self.session.commit()
        try:
            async with ssh_client:
                await AmneziaInstaller(ssh_client).ensure_ready()
        except Exception:
            server.status = ServerStatus.ERROR
            await self.session.commit()
            raise
        else:
            server.status = ServerStatus.READY
            await self.session.commit()

    async def create_ssh_client(self, server_id: UUID) -> SSHClient:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError
        server_credential = await self.credentials.get(server_id)
        auth_type = server_credential.auth_type
        secret = await self.credentials.get_decrypted_secret(server_id)
        auth: dict[str, str | None] = (
            {"password": secret, "private_key": None}
            if auth_type == AuthType.PASSWORD
            else {"password": None, "private_key": secret}
        )

        # Закрепление и несовпадение ключа пишутся в СВОЕЙ транзакции: запись должна сохраниться, даже если
        # вызывающая операция потом откатится или упадёт (а несовпадение её как раз и роняет).
        async def learned(key: HostKey) -> None:
            async with session_factory() as session:
                await ServerHostKeyService(session, SYSTEM_ACTOR).learned(server_id, key)

        async def mismatch(error: SSHHostKeyError) -> None:
            async with session_factory() as session:
                await ServerHostKeyService(session, SYSTEM_ACTOR).mismatch(server_id, error)

        return SSHClient(
            host=server.host,
            username=server_credential.username,
            port=server.ssh_port,
            password=auth["password"],
            private_key=auth["private_key"],
            retries=3,
            retry_delay=1.0,
            connect_timeout=settings.ssh_connect_timeout,
            command_timeout=settings.ssh_command_timeout,
            host_key=pinned_host_key(server),
            on_host_key_learned=learned,
            on_host_key_mismatch=mismatch,
        )

    async def protocol_install(self, protocol: VPNProtocol) -> VPNProtocol:
        ssh_client = await self.create_ssh_client(protocol.server_id)
        provider = self.build_provider(ssh_client, protocol.type)
        async with ssh_client:
            status = await provider.install()
        protocol.port = status.port
        await self.protocols.create(protocol)
        await self.session.commit()
        return protocol

    async def protocol_uninstall(
        self, server_id: UUID, protocol_type: ProtocolType = ProtocolType.AMNEZIA_WG
    ) -> ProviderStatus:
        ssh_client = await self.create_ssh_client(server_id)
        provider = self.build_provider(ssh_client, protocol_type)
        try:
            async with ssh_client:
                status = await provider.uninstall()
        except Exception:
            await self.session.commit()
            raise
        else:
            protocol = VPNProtocol(server_id=server_id, type=protocol_type, port=status.port)
            await self.protocols.delete(protocol)
            await self.session.commit()
        return status
