#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.protocols.models.protocol import ProtocolType, VPNProtocol


class ProtocolRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, protocol_id: UUID) -> VPNProtocol | None:
        return await self.session.get(VPNProtocol, protocol_id)

    async def get_by_server_and_type(self, server_id: UUID, protocol_type: ProtocolType) -> VPNProtocol | None:
        query = select(VPNProtocol).where(VPNProtocol.server_id == server_id, VPNProtocol.type == protocol_type)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def list_by_server_id(self, server_id: UUID) -> Sequence[VPNProtocol]:
        result = await self.session.execute(select(VPNProtocol).where(VPNProtocol.server_id == server_id))
        return result.scalars().all()

    async def create(self, protocol: VPNProtocol) -> VPNProtocol:
        old_protocol = await self.get_by_server_and_type(protocol.server_id, protocol.type)
        if old_protocol:
            old_protocol.port = protocol.port
            return old_protocol
        else:
            self.session.add(protocol)
            return protocol

    async def delete(self, protocol: VPNProtocol) -> None:
        old_protocol = await self.get_by_server_and_type(protocol.server_id, protocol.type)
        if not old_protocol:
            return
        await self.session.delete(old_protocol)
