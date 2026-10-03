#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import AdminRuleError, AlreadyExistsError, NotFoundError
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.admin_service import AdminService
from modules.auth.dependencies import require
from modules.auth.models.admin import Admin
from modules.auth.permissions import Permission
from modules.auth.schemas import AdminCreate, AdminRead, AdminUpdate

router = APIRouter(prefix="/api/v1/admins", tags=["admins"])
ADMINS_MANAGE = [Depends(require(Permission.ADMINS_MANAGE))]


def get_admin_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> AdminService:
    return AdminService(session, actor)


ServiceDep = Annotated[AdminService, Depends(get_admin_service)]


@router.get("", dependencies=ADMINS_MANAGE, response_model=list[AdminRead])
async def list_admins(service: ServiceDep) -> Sequence[Admin]:
    return await service.list_all()


@router.post("", dependencies=ADMINS_MANAGE, response_model=AdminRead, status_code=201)
async def create_admin(body: AdminCreate, service: ServiceDep) -> Admin:
    try:
        return await service.create(body)
    except AlreadyExistsError:
        raise HTTPException(409, detail="Admin with this email already exists")


@router.get("/{admin_id}", dependencies=ADMINS_MANAGE, response_model=AdminRead)
async def get_admin(admin_id: UUID, service: ServiceDep) -> Admin:
    try:
        return await service.get(admin_id)
    except NotFoundError:
        raise HTTPException(404, detail="Admin not found")


@router.patch("/{admin_id}", dependencies=ADMINS_MANAGE, response_model=AdminRead)
async def update_admin(admin_id: UUID, body: AdminUpdate, service: ServiceDep) -> Admin:
    try:
        return await service.update(admin_id, body)
    except NotFoundError:
        raise HTTPException(404, detail="Admin not found")
    except AdminRuleError as exc:
        raise HTTPException(409, detail=str(exc))
