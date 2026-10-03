#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import AlreadyExistsError, CredentialNotFound, NotFoundError
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.dependencies import get_current_admin
from modules.jobs.api.v1.router import get_job_service
from modules.jobs.models.job import Job, JobType
from modules.jobs.schemas import JobRead
from modules.jobs.service import JobService
from modules.protocols.models.protocol import VPNProtocol
from modules.protocols.schemas import ProtocolRead
from modules.servers.models.credential import ServerCredential
from modules.servers.models.server import VPNServer
from modules.servers.schemas import (
    CredentialCreate,
    CredentialRead,
    ServerCreate,
    ServerRead,
    ServerUpdate,
    TestConnectionResult,
)
from modules.servers.service import CredentialService, ServerConnectionService, ServerService

router = APIRouter(prefix="/api/v1/servers", tags=["servers"], dependencies=[Depends(get_current_admin)])


def get_server_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> ServerService:
    return ServerService(session, actor)


def get_credential_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> CredentialService:
    return CredentialService(session, actor)


def get_connection_service(session: Annotated[AsyncSession, Depends(get_session)]) -> ServerConnectionService:
    return ServerConnectionService(session)


@router.post("", response_model=ServerRead, status_code=201)
async def create(body: ServerCreate, service: ServerService = Depends(get_server_service)) -> VPNServer:
    try:
        server = await service.create(body)
    except AlreadyExistsError:
        raise HTTPException(409, detail=f"Server with name '{body.name}' already exist")
    return server


@router.get("", response_model=list[ServerRead])
async def list_all(service: ServerService = Depends(get_server_service)) -> Sequence[VPNServer]:
    return await service.list_all()


@router.get("/{server_id}", response_model=ServerRead)
async def get(server_id: UUID, service: ServerService = Depends(get_server_service)) -> VPNServer:
    try:
        server = await service.get(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    return server


@router.patch("/{server_id}", response_model=ServerRead)
async def patch(server_id: UUID, data: ServerUpdate, service: ServerService = Depends(get_server_service)) -> VPNServer:
    try:
        server = await service.update(server_id, data)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    return server


@router.delete("/{server_id}", status_code=204)
async def delete(server_id: UUID, service: ServerService = Depends(get_server_service)) -> None:
    try:
        await service.delete(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")


@router.put("/{server_id}/credentials", response_model=CredentialRead, status_code=200)
async def put(
    body: CredentialCreate, server_id: UUID, service: CredentialService = Depends(get_credential_service)
) -> ServerCredential:
    try:
        cred = await service.set_credential(server_id, body)
    except NotFoundError:
        raise HTTPException(404, detail=f"Server '{server_id}' not found")
    return cred


@router.get("/{server_id}/credentials", response_model=CredentialRead, status_code=200)
async def get_credential(
    server_id: UUID, service: CredentialService = Depends(get_credential_service)
) -> ServerCredential:
    try:
        cred = await service.get(server_id)
    except CredentialNotFound:
        raise HTTPException(404, detail=f"Credential for server '{server_id}' not found")
    return cred


@router.delete("/{server_id}/credentials", status_code=204)
async def del_credential(server_id: UUID, service: CredentialService = Depends(get_credential_service)) -> None:
    try:
        await service.delete(server_id)
    except CredentialNotFound:
        raise HTTPException(404, detail=f"Credential for server '{server_id}' not found")


@router.post("/{server_id}/test-connection", response_model=TestConnectionResult, status_code=200)
async def test_connection(
    server_id: UUID, service: ServerConnectionService = Depends(get_connection_service)
) -> TestConnectionResult:
    try:
        result = await service.test_connection(server_id)
    except NotFoundError:
        raise HTTPException(404, detail=f"Credentials for server {server_id} not set")
    return result


@router.post("/{server_id}/install", response_model=JobRead, status_code=202)
async def server_install(
    server_id: UUID,
    server_service: ServerService = Depends(get_server_service),
    job_service: JobService = Depends(get_job_service),
    cred_service: CredentialService = Depends(get_credential_service),
) -> Job:
    try:
        await server_service.get(server_id)
        await cred_service.get(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")

    job = await job_service.create(job_type=JobType.INSTALL_SERVER, server_id=server_id)
    return job


@router.post("/{server_id}/protocol", response_model=JobRead, status_code=202)
async def protocol_install(
    server_id: UUID,
    server_service: ServerService = Depends(get_server_service),
    job_service: JobService = Depends(get_job_service),
    cred_service: CredentialService = Depends(get_credential_service),
) -> Job:
    try:
        await server_service.get(server_id)
        await cred_service.get(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")

    job = await job_service.create(job_type=JobType.INSTALL_PROTOCOL, server_id=server_id)
    return job


@router.get("/{server_id}/protocols", response_model=list[ProtocolRead])
async def list_protocols(
    server_id: UUID, service: ServerService = Depends(get_server_service)
) -> Sequence[VPNProtocol]:
    try:
        return await service.list_protocols(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")


@router.post("/{server_id}/sync", response_model=JobRead, status_code=202)
async def server_sync(
    server_id: UUID,
    server_service: ServerService = Depends(get_server_service),
    job_service: JobService = Depends(get_job_service),
    cred_service: CredentialService = Depends(get_credential_service),
) -> Job:
    # Отчёт сверки появится в поле result задачи (GET /api/v1/jobs/{id}) после её завершения.
    try:
        await server_service.get(server_id)
        await cred_service.get(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")

    return await job_service.create(job_type=JobType.SYNC_SERVER, server_id=server_id)


@router.post("/{server_id}/health", response_model=JobRead, status_code=202)
async def server_health(
    server_id: UUID,
    server_service: ServerService = Depends(get_server_service),
    job_service: JobService = Depends(get_job_service),
    cred_service: CredentialService = Depends(get_credential_service),
) -> Job:
    # Полный отчёт — в result задачи; сводка (health_status, last_health_at) — в GET /servers/{id}.
    try:
        await server_service.get(server_id)
        await cred_service.get(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")

    return await job_service.create(job_type=JobType.HEALTH_CHECK, server_id=server_id)


@router.delete("/{server_id}/protocol", response_model=JobRead, status_code=202)
async def protocol_uninstall(
    server_id: UUID,
    server_service: ServerService = Depends(get_server_service),
    job_service: JobService = Depends(get_job_service),
    cred_service: CredentialService = Depends(get_credential_service),
) -> Job:
    try:
        await server_service.get(server_id)
        await cred_service.get(server_id)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")

    job = await job_service.create(job_type=JobType.REMOVE_PROTOCOL, server_id=server_id)
    return job
