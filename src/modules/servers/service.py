#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AlreadyExistsError, NotFoundError
from core.security import decrypt_secret, encrypt_secret
from modules.servers.models.credential import ServerCredential
from modules.servers.models.server import VPNServer
from modules.servers.repository import CredentialRepository, ServerRepository
from modules.servers.schemas import CredentialCreate, ServerCreate, ServerUpdate


class ServerService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.servers = ServerRepository(session)

    async def create(self, data: ServerCreate) -> VPNServer:
        if await self.servers.get_by_name(data.name):
            raise AlreadyExistsError
        new_server = VPNServer(name=data.name, host=data.host, ssh_port=data.ssh_port, description=data.description)
        server = await self.servers.create(new_server)
        await self.session.commit()
        return server

    async def get(self, server_id: UUID) -> VPNServer:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError
        return server

    async def list_all(self) -> Sequence[VPNServer]:
        return await self.servers.list_all()

    async def update(self, server_id: UUID, data: ServerUpdate) -> VPNServer:
        server = await self.get(server_id)
        fields = data.model_dump(exclude_unset=True)
        for field, value in fields.items():
            setattr(server, field, value)
        await self.session.commit()
        return server

    async def delete(self, server_id: UUID) -> None:
        server = await self.get(server_id)
        await self.servers.delete(server)
        await self.session.commit()


class CredentialService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.credentials = CredentialRepository(session)
        self.servers = ServerRepository(session)

    async def set_credential(self, server_id: UUID, data: CredentialCreate) -> ServerCredential:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError
        encrypted_secret = encrypt_secret(data.secret)
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
            raise NotFoundError
        return cred

    async def delete(self, server_id: UUID) -> None:
        cred = await self.credentials.get_by_server_id(server_id)
        if not cred:
            raise NotFoundError
        await self.credentials.delete(cred)
        await self.session.commit()

    async def get_decrypted_secret(self, server_id: UUID) -> str:
        cred = await self.credentials.get_by_server_id(server_id)
        if not cred:
            raise NotFoundError
        return decrypt_secret(cred.secret_encrypted)
