#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.servers.models.server import VPNServer


class ServerRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, server: VPNServer) -> VPNServer:
        self.session.add(server)
        await self.session.flush()
        return server

    async def get_by_id(self, server_id: UUID) -> VPNServer | None:
        return await self.session.get(VPNServer, server_id)

    async def get_by_name(self, server_name: str) -> VPNServer | None:
        sql_request = select(VPNServer).where(VPNServer.name == server_name)
        server = await self.session.execute(sql_request)
        return server.scalar_one_or_none()

    async def list_all(self) -> Sequence[VPNServer]:
        sql_request = select(VPNServer)
        server_list = await self.session.execute(sql_request)
        return server_list.scalars().all()

    async def delete(self, server: VPNServer) -> None:
        await self.session.delete(server)
