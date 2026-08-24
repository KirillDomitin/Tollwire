#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AlreadyExistsError, NotFoundError
from modules.servers.models.server import VPNServer
from modules.servers.repository import ServerRepository
from modules.servers.schemas import ServerCreate, ServerUpdate


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
