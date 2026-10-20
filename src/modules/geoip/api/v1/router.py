#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.postgres import get_session
from core.exceptions import InvalidDataError, JobAlreadyRunningError, NotFoundError, StateConflictError
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.dependencies import get_current_admin, require
from modules.auth.permissions import Permission
from modules.geoip.models.geoip import GeoipList, GeoipOverride
from modules.geoip.schemas import GeoipIngestResult, GeoipListRead, GeoipOverview, OverrideCreate, OverrideRead
from modules.geoip.service import GeoipService
from modules.jobs.models.job import Job
from modules.jobs.schemas import JobRead

router = APIRouter(prefix="/api/v1/geoip", tags=["geoip"], dependencies=[Depends(get_current_admin)])


def get_geoip_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> GeoipService:
    return GeoipService(session, actor)


Service = Annotated[GeoipService, Depends(get_geoip_service)]


@router.get("", dependencies=[Depends(require(Permission.SERVERS_READ))], response_model=GeoipOverview)
async def overview(service: Service) -> GeoipOverview:
    """Активный список (версия, число префиксов, sha256) и кандидаты, ждущие подтверждения. Самих префиксов нет."""
    return await service.overview()


@router.post(
    "/update", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=JobRead, status_code=202
)
async def update(service: Service) -> Job:
    """Скачивает список с RIPE (адрес - из настроек панели) в фоне; итог - в result задачи `geoip_update`."""
    try:
        return await service.schedule_update()
    except JobAlreadyRunningError as exc:
        raise HTTPException(409, detail=str(exc))


@router.post("/upload", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=GeoipIngestResult)
async def upload(request: Request, service: Service) -> GeoipIngestResult:
    """Запасной путь без доступа к RIPE: тело `text/plain` - файл RIPE или CIDR по строке, до 5 МБ. Те же проверки и
    правила активации, что у скачанного списка."""
    limit = settings.geoip_upload_max_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, detail=f"The list is larger than {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, detail=f"The list is larger than {limit} bytes")
    try:
        result = await service.upload(bytes(body).decode("utf-8", errors="replace"))
    except InvalidDataError as exc:
        raise HTTPException(422, detail=str(exc))
    return GeoipIngestResult(**result)


@router.get("/overrides", dependencies=[Depends(require(Permission.SERVERS_READ))], response_model=list[OverrideRead])
async def list_overrides(service: Service) -> Sequence[GeoipOverride]:
    return await service.list_overrides()


@router.post(
    "/overrides",
    dependencies=[Depends(require(Permission.SERVERS_MANAGE))],
    response_model=OverrideRead,
    status_code=201,
)
async def add_override(body: OverrideCreate, service: Service) -> GeoipOverride:
    """Сеть, которая идёт напрямую (`direct`) или через выход (`tunnel`) вопреки списку. Только публичный IPv4 CIDR."""
    try:
        return await service.add_override(body)
    except InvalidDataError as exc:
        raise HTTPException(422, detail=str(exc))
    except StateConflictError as exc:
        raise HTTPException(409, detail=str(exc))


@router.delete("/overrides/{override_id}", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], status_code=204)
async def remove_override(override_id: UUID, service: Service) -> None:
    try:
        await service.remove_override(override_id)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))


@router.post(
    "/{list_id}/activate", dependencies=[Depends(require(Permission.SERVERS_MANAGE))], response_model=GeoipListRead
)
async def activate(list_id: UUID, service: Service) -> GeoipList:
    """Подтверждает кандидата (он отличался от активного больше чем на 20%): список становится активным."""
    try:
        return await service.activate(list_id)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except StateConflictError as exc:
        raise HTTPException(409, detail=str(exc))
