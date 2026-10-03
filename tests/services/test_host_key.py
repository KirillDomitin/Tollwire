"""Закрепление ключа SSH-сервера в БД: TOFU, гонка двух подключений, несовпадение, связка с SSH-клиентом."""

import asyncssh
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import SSHHostKeyError
from infrastructure.ssh.host_keys import HostKey
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.servers.host_key import ServerHostKeyService
from modules.servers.models.server import ServerStatus, VPNServer
from modules.servers.schemas import CredentialCreate, ServerCreate
from modules.servers.service import CredentialService, ServerConnectionService, ServerService


def new_key() -> HostKey:
    return HostKey.from_ssh_key(asyncssh.generate_private_key("ssh-ed25519").convert_to_public())


async def make_server(session: AsyncSession) -> VPNServer:
    server = await ServerService(session, SYSTEM_ACTOR).create(ServerCreate(name="srv", host="203.0.113.10"))
    await CredentialService(session, SYSTEM_ACTOR).set_credential(server.id, CredentialCreate(secret="pw"))
    return server


async def audit(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    return list((await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


async def test_first_connection_pins_key_once(session: AsyncSession) -> None:
    server = await make_server(session)
    key = new_key()
    service = ServerHostKeyService(session, SYSTEM_ACTOR)
    await service.learned(server.id, key)
    await service.learned(server.id, key)  # тот же ключ повторно — ничего не меняется
    await session.refresh(server)
    assert (server.ssh_host_key, server.ssh_host_key_fingerprint) == (key.public_key, key.fingerprint)
    (pinned,) = await audit(session, AuditAction.SERVER_HOST_KEY_PINNED)
    assert pinned.meta == {"fingerprint": key.fingerprint, "previous": None, "source": "tofu"}


async def test_race_with_different_key_is_mismatch(session: AsyncSession) -> None:
    """Два подключения без ключа одновременно, второе увидело ДРУГОЙ ключ: закрепляется первый, второе — тревога."""
    server = await make_server(session)
    first, second = new_key(), new_key()
    service = ServerHostKeyService(session, SYSTEM_ACTOR)
    await service.learned(server.id, first)
    with pytest.raises(SSHHostKeyError) as exc:
        await service.learned(server.id, second)
    assert (exc.value.expected, exc.value.presented) == (first.fingerprint, second.fingerprint)
    await session.refresh(server)
    assert server.ssh_host_key == first.public_key and server.status == ServerStatus.ERROR
    (alarm,) = await audit(session, AuditAction.SERVER_HOST_KEY_MISMATCH)
    assert alarm.meta == {"expected": first.fingerprint, "presented": second.fingerprint, "host": "203.0.113.10"}


async def test_connection_service_wires_pinned_key_and_callbacks(session: AsyncSession) -> None:
    server = await make_server(session)
    connections = ServerConnectionService(session)

    client = await connections.create_ssh_client(server.id)
    assert client.host_key is None  # ещё не закреплён → TOFU
    key = new_key()
    assert client._on_host_key_learned is not None
    await client._on_host_key_learned(key)  # так его вызовет SSHClient после рукопожатия

    await session.refresh(server)  # подтянуть ключ, закреплённый в другой транзакции
    client = await connections.create_ssh_client(server.id)
    assert client.host_key == key  # теперь подключения идут только с этим ключом

    assert client._on_host_key_mismatch is not None
    await client._on_host_key_mismatch(SSHHostKeyError(server.host, key.fingerprint, "SHA256:evil"))
    await session.refresh(server)
    assert server.status == ServerStatus.ERROR
