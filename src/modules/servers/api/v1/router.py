#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import AlreadyExistsError, CredentialNotFound, LiveCredentialExistsError, NotFoundError, SSHError
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.dependencies import get_current_admin, require
from modules.auth.permissions import Permission
from modules.jobs.models.job import Job, JobType
from modules.jobs.schemas import JobRead
from modules.protocols.models.protocol import VPNProtocol
from modules.protocols.schemas import ProtocolRead
from modules.servers.host_key import ServerHostKeyService
from modules.servers.models.credential import ServerCredential
from modules.servers.models.server import VPNServer
from modules.servers.schemas import (
    CredentialCreate,
    CredentialRead,
    HostKeyPin,
    HostKeyScan,
    ServerCreate,
    ServerRead,
    ServerUpdate,
    SshKeyInstall,
    TestConnectionResult,
)
from modules.servers.service import CredentialService, ServerConnectionService, ServerService
from modules.servers.ssh_key import ServerSshKeyService

router = APIRouter(prefix="/api/v1/servers", tags=["servers"], dependencies=[Depends(get_current_admin)])


def get_server_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> ServerService:
    return ServerService(session, actor)


def get_credential_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> CredentialService:
    return CredentialService(session, actor)


async def schedule(service: ServerService, server_id: UUID, job_type: JobType) -> Job:
    try:
        return await service.schedule(server_id, job_type)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")
    except LiveCredentialExistsError as exc:
        raise HTTPException(409, detail=str(exc))


def get_host_key_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> ServerHostKeyService:
    return ServerHostKeyService(session, actor)


def get_ssh_key_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> ServerSshKeyService:
    return ServerSshKeyService(session, actor)


def get_connection_service(session: Annotated[AsyncSession, Depends(get_session)]) -> ServerConnectionService:
    return ServerConnectionService(session)


@router.post("", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=ServerRead, status_code=201)
async def create(body: ServerCreate, service: ServerService = Depends(get_server_service)) -> VPNServer:
    try:
        server = await service.create(body)
    except AlreadyExistsError:
        raise HTTPException(409, detail=f"Server with name '{body.name}' already exist")
    return server


@router.get("", dependencies=[Depends(require(Permission.SERVERS_READ))], response_model=list[ServerRead])
async def list_all(service: ServerService = Depends(get_server_service)) -> Sequence[VPNServer]:
    return await service.list_all()


@router.get("/{server_id}", dependencies=[Depends(require(Permission.SERVERS_READ))], response_model=ServerRead)
async def get(server_id: UUID, service: ServerService = Depends(get_server_service)) -> VPNServer:
    try:
        server = await service.get(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    return server


@router.patch("/{server_id}", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=ServerRead)
async def patch(server_id: UUID, data: ServerUpdate, service: ServerService = Depends(get_server_service)) -> VPNServer:
    try:
        server = await service.update(server_id, data)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    return server


@router.delete("/{server_id}", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], status_code=204)
async def delete(server_id: UUID, service: ServerService = Depends(get_server_service)) -> None:
    try:
        await service.delete(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")


@router.put(
    "/{server_id}/credentials",
    dependencies=[Depends(require(Permission.SERVERS_MANAGE))],
    response_model=CredentialRead,
    status_code=200,
)
async def put(
    body: CredentialCreate, server_id: UUID, service: CredentialService = Depends(get_credential_service)
) -> ServerCredential:
    try:
        cred = await service.set_credential(server_id, body)
    except NotFoundError:
        raise HTTPException(404, detail=f"Server '{server_id}' not found")
    except ValueError:
        raise HTTPException(422, detail="Not a valid unencrypted SSH private key")
    return cred


@router.get(
    "/{server_id}/credentials",
    dependencies=[Depends(require(Permission.SERVERS_READ))],
    response_model=CredentialRead,
    status_code=200,
)
async def get_credential(
    server_id: UUID, service: CredentialService = Depends(get_credential_service)
) -> ServerCredential:
    try:
        cred = await service.get(server_id)
    except CredentialNotFound:
        raise HTTPException(404, detail=f"Credential for server '{server_id}' not found")
    return cred


@router.delete("/{server_id}/credentials", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], status_code=204)
async def del_credential(server_id: UUID, service: CredentialService = Depends(get_credential_service)) -> None:
    try:
        await service.delete(server_id)
    except CredentialNotFound:
        raise HTTPException(404, detail=f"Credential for server '{server_id}' not found")


@router.post(
    "/{server_id}/test-connection",
    dependencies=[Depends(require(Permission.SERVERS_MANAGE))],
    response_model=TestConnectionResult,
    status_code=200,
)
async def test_connection(
    server_id: UUID, service: ServerConnectionService = Depends(get_connection_service)
) -> TestConnectionResult:
    try:
        result = await service.test_connection(server_id)
    except NotFoundError:
        raise HTTPException(404, detail=f"Credentials for server {server_id} not set")
    return result


@router.post(
    "/{server_id}/install",
    dependencies=[Depends(require(Permission.SERVERS_MANAGE))],
    response_model=JobRead,
    status_code=202,
)
async def server_install(server_id: UUID, service: ServerService = Depends(get_server_service)) -> Job:
    return await schedule(service, server_id, JobType.INSTALL_SERVER)


@router.post(
    "/{server_id}/ssh-key",
    dependencies=[Depends(require(Permission.SERVERS_MANAGE))],
    response_model=JobRead,
    status_code=202,
)
async def install_ssh_key(
    server_id: UUID,
    service: Annotated[ServerSshKeyService, Depends(get_ssh_key_service)],
    body: SshKeyInstall | None = None,
) -> Job:
    """Панель ставит свой SSH-ключ и переходит на вход по нему (повторный вызов — новый ключ вместо прежнего).
    Логин и пароль в теле сохраняются как учётка перед постановкой задачи."""
    credential = (
        CredentialCreate(username=body.username, secret=body.password)
        if body is not None and body.username is not None and body.password is not None
        else None
    )
    try:
        return await service.schedule_install(server_id, credential)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")


@router.post(
    "/{server_id}/protocol",
    dependencies=[Depends(require(Permission.SERVERS_MANAGE))],
    response_model=JobRead,
    status_code=202,
)
async def protocol_install(server_id: UUID, service: ServerService = Depends(get_server_service)) -> Job:
    return await schedule(service, server_id, JobType.INSTALL_PROTOCOL)


@router.get(
    "/{server_id}/protocols",
    dependencies=[Depends(require(Permission.SERVERS_READ))],
    response_model=list[ProtocolRead],
)
async def list_protocols(
    server_id: UUID, service: ServerService = Depends(get_server_service)
) -> Sequence[VPNProtocol]:
    try:
        return await service.list_protocols(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")


@router.post(
    "/{server_id}/sync",
    dependencies=[Depends(require(Permission.SERVERS_CHECK))],
    response_model=JobRead,
    status_code=202,
)
async def server_sync(server_id: UUID, service: ServerService = Depends(get_server_service)) -> Job:
    # Отчёт сверки появится в поле result задачи (GET /api/v1/jobs/{id}) после её завершения.
    return await schedule(service, server_id, JobType.SYNC_SERVER)


@router.post(
    "/{server_id}/health",
    dependencies=[Depends(require(Permission.SERVERS_CHECK))],
    response_model=JobRead,
    status_code=202,
)
async def server_health(server_id: UUID, service: ServerService = Depends(get_server_service)) -> Job:
    # Полный отчёт — в result задачи; сводка (health_status, last_health_at) — в GET /servers/{id}.
    return await schedule(service, server_id, JobType.HEALTH_CHECK)


@router.delete(
    "/{server_id}/protocol",
    dependencies=[Depends(require(Permission.SERVERS_MANAGE))],
    response_model=JobRead,
    status_code=202,
)
async def protocol_uninstall(server_id: UUID, service: ServerService = Depends(get_server_service)) -> Job:
    return await schedule(service, server_id, JobType.REMOVE_PROTOCOL)


@router.post(
    "/{server_id}/host-key/scan", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=HostKeyScan
)
async def scan_host_key(
    server_id: UUID, service: Annotated[ServerHostKeyService, Depends(get_host_key_service)]
) -> HostKeyScan:
    """Ключ, который сервер предъявляет сейчас, — без входа на сервер (пароль не отправляется)."""
    try:
        return await service.scan(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except SSHError as exc:
        raise HTTPException(502, detail=str(exc))


@router.put(
    "/{server_id}/host-key", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=ServerRead
)
async def pin_host_key(
    server_id: UUID, body: HostKeyPin, service: Annotated[ServerHostKeyService, Depends(get_host_key_service)]
) -> VPNServer:
    try:
        return await service.pin(server_id, body.public_key)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except ValueError:
        raise HTTPException(422, detail="Not a valid SSH public key")


@router.delete(
    "/{server_id}/host-key", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=ServerRead
)
async def reset_host_key(
    server_id: UUID, service: Annotated[ServerHostKeyService, Depends(get_host_key_service)]
) -> VPNServer:
    try:
        return await service.reset(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
