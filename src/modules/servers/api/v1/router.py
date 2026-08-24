#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import AlreadyExistsError, NotFoundError
from modules.auth.dependencies import get_current_admin
from modules.servers.models.server import VPNServer
from modules.servers.schemas import ServerCreate, ServerRead, ServerUpdate
from modules.servers.service import ServerService

router = APIRouter(prefix="/api/v1/servers", tags=["servers"], dependencies=[Depends(get_current_admin)])


def get_server_service(session: Annotated[AsyncSession, Depends(get_session)]) -> ServerService:
    return ServerService(session)


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
