#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.security import decode_access_token
from modules.auth.models.admin import Admin
from modules.auth.repository import AdminRepository

security = HTTPBearer()


async def get_current_admin(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Admin:
    access_token = credentials.credentials
    payload = decode_access_token(access_token)
    if not payload:
        raise HTTPException(401)
    admin_id = UUID(str(payload["sub"]))
    admin_repo = AdminRepository(session)
    admin = await admin_repo.get_by_id(admin_id)
    if not admin or not admin.is_active:
        raise HTTPException(401)
    return admin
