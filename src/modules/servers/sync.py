#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError, SSHConnectionError, SSHTimeoutError
from core.security import decrypt_secret
from core.ssh_exception import CommandExecutionError, UnsupportedOperationError
from infrastructure.vpn.amnezia import AmneziaInstaller
from infrastructure.vpn.provider import ProviderState, VPNClientInfo, VPNProvider
from modules.access.models import CredentialSource
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import VPNCredentialRepository
from modules.access.service import CLIENT_NAME_PREFIX, client_name, inactive_reason
from modules.protocols.models.protocol import APP_PROTOCOLS, ProtocolType, VPNProtocol
from modules.protocols.repository import ProtocolRepository
from modules.servers.reachability import mark_reachable, mark_unreachable
from modules.servers.repository import ServerRepository
from modules.servers.service import ServerConnectionService


def _client(info: VPNClientInfo) -> dict[str, Any]:
    return {"name": info.name, "address": info.address, "public_key": info.public_key}


def _credential(credential: VPNCredential) -> dict[str, Any]:
    return {
        "credential_id": str(credential.id),
        "address": credential.client_address,
        "public_key": credential.client_public_key,
    }


class ServerSyncService:
    """Сверяет реальное состояние сервера с БД и чинит клиентов по БД (PostgreSQL — источник правды).

    Вызывается воркером под ServerLock. Отчёт — JSON без секретов, он сохраняется в Job.result.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.servers = ServerRepository(session)
        self.protocols = ProtocolRepository(session)
        self.credentials = VPNCredentialRepository(session)
        self.connections = ServerConnectionService(session)

    async def sync(self, server_id: UUID) -> dict[str, Any]:
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError(f"Server '{server_id}' not found")
        ssh_client = await self.connections.create_ssh_client(server_id)

        try:
            await ssh_client.connect()
        except (SSHConnectionError, SSHTimeoutError):
            mark_unreachable(server)
            await self.session.commit()
            raise

        async with ssh_client:
            docker = await AmneziaInstaller(ssh_client).docker_version()
            mark_reachable(server, docker_installed=docker is not None)
            protocols = []
            if docker:
                tracked = {protocol.type: protocol for protocol in await self.protocols.list_by_server_id(server_id)}
                for protocol_type in ProtocolType:
                    provider = self.connections.build_provider(ssh_client, protocol_type)
                    report = await self._sync_protocol(provider, protocol_type, tracked.get(protocol_type))
                    if report is not None:
                        protocols.append(report)
            await self.session.commit()

        return {
            "ssh": True,
            "docker": docker,
            "server_status": server.status.value,
            "protocols": protocols,
            "issues": [] if docker else ["Docker is not installed"],
        }

    async def _sync_protocol(
        self, provider: VPNProvider, protocol_type: ProtocolType, protocol: VPNProtocol | None
    ) -> dict[str, Any] | None:
        snapshot = await provider.sync()
        state = snapshot.provider_status.server_status
        if protocol is None and state == ProviderState.NOT_INSTALLED and protocol_type != ProtocolType.AMNEZIA_WG:
            return None
        report: dict[str, Any] = {
            "type": protocol_type.value,
            "in_db": protocol is not None,
            "state": state.value,
            "port": snapshot.provider_status.port,
            "clients_on_server": len(snapshot.client_list),
            "issues": [],
        }

        if protocol is None:
            if state != ProviderState.NOT_INSTALLED:
                report["issues"].append("Installed on server but not tracked in DB")
            return report
        if state == ProviderState.NOT_INSTALLED:
            report["issues"].append("Tracked in DB but not installed on server")
            return report

        real_port = snapshot.provider_status.port
        if real_port is not None and protocol.port != real_port:
            report["port_updated_from"] = protocol.port
            protocol.port = real_port

        if state != ProviderState.RUNNING:
            report["issues"].append("Container is not running, clients were not reconciled")
            return report

        report.update(await self._reconcile_clients(provider, protocol, snapshot.client_list, report["issues"]))
        return report

    async def _reconcile_clients(
        self, provider: VPNProvider, protocol: VPNProtocol, on_server: list[VPNClientInfo], issues: list[str]
    ) -> dict[str, Any]:
        live: dict[str, tuple[VPNCredential, UUID]] = {}
        stale: list[VPNCredential] = []
        revoked_keys: set[str] = set()
        now = datetime.now(timezone.utc)
        for credential, grant, subscription in await self.credentials.list_with_access_by_protocol_id(protocol.id):
            if credential.revoked_at is not None:
                revoked_keys.add(credential.client_public_key)
            elif inactive_reason(grant, subscription, now):
                stale.append(credential)
            else:
                live[credential.client_public_key] = (credential, grant.id)

        server_keys = {client.public_key for client in on_server}
        result: dict[str, list[dict[str, Any]]] = {
            "ok": [],
            "restored": [],
            "removed_revoked": [],
            "removed_orphans": [],
            "revocations_completed": [],
            "foreign": [],
        }

        # Доступ уже закончился, а REVOKE_ACCESS не дошёл до конца (например, сервер был недоступен).
        for credential in stale:
            if credential.client_public_key in server_keys:
                await provider.revoke_client(credential.client_public_key)
            credential.revoked_at = datetime.now(timezone.utc)
            await self.session.commit()
            result["revocations_completed"].append(_credential(credential))
        stale_keys = {credential.client_public_key for credential in stale}

        for client in on_server:
            if client.public_key in live:
                result["ok"].append(_client(client))
            elif client.public_key in stale_keys:
                continue
            elif client.public_key in revoked_keys:
                await provider.revoke_client(client.public_key)
                result["removed_revoked"].append(_client(client))
            elif client.name and client.name.startswith(CLIENT_NAME_PREFIX) and protocol.type not in APP_PROTOCOLS:
                # Наш peer без записи в БД: упал commit после create_client или запись удалили.
                await provider.revoke_client(client.public_key)
                result["removed_orphans"].append(_client(client))
            else:
                # Добавлен на сервер не панелью — не трогаем, только сообщаем.
                result["foreign"].append(_client(client))

        for public_key, (credential, access_grant_id) in live.items():
            if public_key in server_keys:
                continue
            # Секреты (конфиг, PSK) расшифровываются только в памяти и уходят провайдеру; в отчёт и логи не попадают.
            try:
                if credential.source == CredentialSource.IMPORTED:
                    # Конфига нет (приватный ключ остался в приложении клиента): peer возвращается по PSK и адресу.
                    if credential.encrypted_psk is None:
                        issues.append(f"Credential {credential.id} was not restored: no PSK stored")
                        continue
                    await provider.restore_peer(
                        client_name(access_grant_id),
                        credential.client_public_key,
                        decrypt_secret(credential.encrypted_psk),
                        credential.client_address,
                    )
                else:
                    if credential.encrypted_config is None:  # CHECK в БД этого не допускает; страховка от порчи данных
                        issues.append(f"Credential {credential.id} was not restored: no config stored")
                        continue
                    await provider.restore_client(
                        client_name(access_grant_id), decrypt_secret(credential.encrypted_config)
                    )
            except (CommandExecutionError, UnsupportedOperationError, ValueError) as exc:
                # Один битый клиент (адрес занят чужим peer'ом, повреждённый конфиг) не должен срывать весь sync.
                issues.append(f"Credential {credential.id} was not restored: {exc}")
                continue
            result["restored"].append(_credential(credential))

        return result
