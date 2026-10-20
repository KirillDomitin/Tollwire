#!/usr/bin/python
# -*- coding: utf-8 -*-
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from core.security import decrypt_secret
from infrastructure.vpn.gateway_config import ExitTunnel, format_endpoint
from modules.gateway.models.gateway_exit import GatewayExit
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import ServerRepository


async def exit_tunnel(session: AsyncSession, gateway_exit: GatewayExit) -> ExitTunnel:
    """Туннель шлюза к выходу по строке БД: конфиг внутреннего клиента (расшифровывается в памяти) и Endpoint из адреса
    сервера-выхода и порта его AMNEZIA_WG. Конфиг внутреннего клиента ещё не создан - NotFoundError."""
    protocol = await ProtocolRepository(session).get_by_id(gateway_exit.exit_protocol_id)
    server = await ServerRepository(session).get_by_id(protocol.server_id) if protocol else None
    if protocol is None or server is None or not protocol.port:
        raise NotFoundError("Exit server not found")
    if gateway_exit.encrypted_config is None:
        raise NotFoundError("The internal client of the exit has no stored config")
    return ExitTunnel(
        slot=gateway_exit.slot,
        client_config=decrypt_secret(gateway_exit.encrypted_config),
        endpoint=format_endpoint(server.host, protocol.port),
    )
