#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Annotated, Sequence
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import AlreadyExistsError, LiveCredentialExistsError, NotFoundError
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.dependencies import get_current_admin, require
from modules.auth.permissions import Permission
from modules.vpn_users.models.user import VPNUser
from modules.vpn_users.schemas import VPNUserCreate, VPNUserListItem, VPNUserRead, VPNUserUpdate
from modules.vpn_users.service import VPNUserService

router = APIRouter(prefix="/api/v1/vpn-users", tags=["users"], dependencies=[Depends(get_current_admin)])


def get_user_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> VPNUserService:
    return VPNUserService(session, actor)


@router.get("", dependencies=[Depends(require(Permission.VPN_USERS_READ))], response_model=Sequence[VPNUserListItem])
async def get(service: VPNUserService = Depends(get_user_service)) -> list[VPNUserListItem]:
    return await service.list_with_access()


@router.post(
    "", dependencies=[Depends(require(Permission.VPN_USERS_MANAGE))], response_model=VPNUserRead, status_code=201
)
async def create(body: VPNUserCreate, service: VPNUserService = Depends(get_user_service)) -> VPNUser:
    try:
        user = await service.create(body)
    except AlreadyExistsError:
        raise HTTPException(409, detail=f"User with email '{body.email}' already exist")
    return user


@router.get("/{user_id}", dependencies=[Depends(require(Permission.VPN_USERS_READ))], response_model=VPNUserRead)
async def get_by_id(user_id: UUID, service: VPNUserService = Depends(get_user_service)) -> VPNUser:
    try:
        user = await service.get(user_id)
    except NotFoundError:
        raise HTTPException(404, detail="User not found")
    return user


@router.patch("/{user_id}", dependencies=[Depends(require(Permission.VPN_USERS_MANAGE))], response_model=VPNUserRead)
async def patch(user_id: UUID, data: VPNUserUpdate, service: VPNUserService = Depends(get_user_service)) -> VPNUser:
    try:
        user = await service.update(user_id, data)
    except NotFoundError:
        raise HTTPException(404, detail="User not found")
    return user


@router.delete("/{user_id}", dependencies=[Depends(require(Permission.VPN_USERS_MANAGE))], status_code=204)
async def delete(user_id: UUID, service: VPNUserService = Depends(get_user_service)) -> None:
    try:
        await service.delete(user_id)
    except NotFoundError:
        raise HTTPException(404, detail="User not found")
    except LiveCredentialExistsError as exc:
        raise HTTPException(409, detail=str(exc))
