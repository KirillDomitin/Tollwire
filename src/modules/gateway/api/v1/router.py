#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import (
    CredentialNotFound,
    GatewayExitError,
    InvalidDataError,
    JobAlreadyRunningError,
    NotFoundError,
)
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.dependencies import get_current_admin, require
from modules.auth.permissions import Permission
from modules.gateway.ru import GatewayRuService
from modules.gateway.schemas import GatewayAssignmentRead, GatewayExitCreate, GatewayExitRead, GatewayExitUpdate
from modules.gateway.service import GatewayExitService
from modules.jobs.models.job import Job
from modules.jobs.schemas import JobRead

router = APIRouter(
    prefix="/api/v1/servers/{server_id}/gateway", tags=["gateway"], dependencies=[Depends(get_current_admin)]
)


def get_gateway_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> GatewayExitService:
    return GatewayExitService(session, actor)


Service = Annotated[GatewayExitService, Depends(get_gateway_service)]


@router.get("/exits", dependencies=[Depends(require(Permission.SERVERS_READ))], response_model=list[GatewayExitRead])
async def list_exits(server_id: UUID, service: Service) -> list[GatewayExitRead]:
    """Выходы шлюза по слотам. Ключей и конфигов внутренних клиентов в ответе нет."""
    try:
        return await service.list_exits(server_id)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))


@router.post(
    "/exits", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=JobRead, status_code=202
)
async def add_exit(server_id: UUID, body: GatewayExitCreate, service: Service) -> Job:
    """Подключает зарубежный сервер выходом шлюза: задача `gateway_add_exit` (внутренний клиент на выходе, затем
    туннель на шлюзе). Состояние выхода - в `GET /exits`."""
    try:
        return await service.schedule_add(server_id, body)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")
    except InvalidDataError as exc:
        raise HTTPException(422, detail=str(exc))
    except (GatewayExitError, JobAlreadyRunningError) as exc:
        raise HTTPException(409, detail=str(exc))


@router.patch(
    "/exits/{exit_id}", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=GatewayExitRead
)
async def update_exit(server_id: UUID, exit_id: UUID, body: GatewayExitUpdate, service: Service) -> GatewayExitRead:
    """Вес и предел клиентов выхода (только БД, без задачи)."""
    try:
        return await service.update(server_id, exit_id, body)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except InvalidDataError as exc:
        raise HTTPException(422, detail=str(exc))
    except GatewayExitError as exc:
        raise HTTPException(409, detail=str(exc))


@router.delete(
    "/exits/{exit_id}",
    dependencies=[Depends(require(Permission.SERVERS_MANAGE))],
    response_model=JobRead,
    status_code=202,
)
async def remove_exit(server_id: UUID, exit_id: UUID, service: Service, force: Annotated[bool, Query()] = False) -> Job:
    """Снимает выход: задача `gateway_remove_exit` (клиенты переносятся на другие выходы, затем туннель на шлюзе и
    внутренний клиент на выходе). Если часть клиентов останется без выхода (и без интернета) - 409 с их числом;
    `?force=true` подтверждает снятие."""
    try:
        return await service.schedule_remove(server_id, exit_id, force)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")
    except (GatewayExitError, JobAlreadyRunningError) as exc:
        raise HTTPException(409, detail=str(exc))


@router.get(
    "/assignments",
    dependencies=[Depends(require(Permission.SERVERS_READ))],
    response_model=list[GatewayAssignmentRead],
)
async def list_assignments(server_id: UUID, service: Service) -> list[GatewayAssignmentRead]:
    """Какой клиент шлюза идёт через какой выход (credential_id, exit_id, слот, причина, время). Без адресов."""
    try:
        return await service.list_assignments(server_id)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))


@router.post(
    "/ru/apply", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=JobRead, status_code=202
)
async def apply_ru(
    server_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[AuditActor, Depends(get_audit_actor)],
) -> Job:
    """Применяет на шлюзе эффективный список «Россия напрямую» (активный список и исключения): задача
    `gateway_apply_ru`. Нужен активный список (`GET /api/v1/geoip`)."""
    try:
        return await GatewayRuService(session, actor).schedule_apply(server_id)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except CredentialNotFound:
        raise HTTPException(409, detail="Credentials do not exist to perform this operation")
    except (GatewayExitError, JobAlreadyRunningError) as exc:
        raise HTTPException(409, detail=str(exc))
