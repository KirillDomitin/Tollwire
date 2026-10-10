#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import NotFoundError
from modules.auth.dependencies import get_current_admin, require
from modules.auth.permissions import Permission
from modules.monitoring.schemas import LatestMetrics, MetricsRange, MetricsSeries
from modules.monitoring.service import MetricsService

router = APIRouter(prefix="/api/v1", tags=["monitoring"], dependencies=[Depends(get_current_admin)])

READ = [Depends(require(Permission.SERVERS_READ))]


def get_metrics_service(session: Annotated[AsyncSession, Depends(get_session)]) -> MetricsService:
    return MetricsService(session)


@router.get("/servers/{server_id}/monitoring", dependencies=READ, response_model=MetricsSeries)
async def server_metrics(
    server_id: UUID,
    service: Annotated[MetricsService, Depends(get_metrics_service)],
    metrics_range: Annotated[MetricsRange, Query(alias="range")] = MetricsRange.DAY,
) -> MetricsSeries:
    try:
        return await service.series(server_id, metrics_range)
    except NotFoundError:
        raise HTTPException(404, detail="Server not found")


@router.get("/monitoring/latest", dependencies=READ, response_model=list[LatestMetrics])
async def latest_metrics(service: Annotated[MetricsService, Depends(get_metrics_service)]) -> list[LatestMetrics]:
    return await service.latest()
