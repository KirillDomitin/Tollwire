#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import NotFoundError
from modules.auth.dependencies import get_current_admin
from modules.jobs.models.job import Job, JobStatus
from modules.jobs.schemas import JobRead
from modules.jobs.service import JobService

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"], dependencies=[Depends(get_current_admin)])


def get_job_service(session: Annotated[AsyncSession, Depends(get_session)]) -> JobService:
    return JobService(session)


@router.get("/{job_id}", response_model=JobRead)
async def get_by_id(job_id: UUID, service: JobService = Depends(get_job_service)) -> Job:
    try:
        job = await service.get(job_id)
    except NotFoundError:
        raise HTTPException(404, detail="Job not found")
    return job


@router.get("", response_model=list[JobRead])
async def list_all(
    service: JobService = Depends(get_job_service),
    server_id: UUID | None = None,
    status: JobStatus | None = None,
    limit: int = Query(default=50, le=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> Sequence[Job]:
    return await service.list_all(server_id, status, limit, offset)
