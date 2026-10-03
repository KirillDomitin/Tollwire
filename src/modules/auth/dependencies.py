#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.security import decode_access_token
from modules.auth.models.admin import Admin
from modules.auth.permissions import Permission, has_permission
from modules.auth.repository import AdminRepository

logger = logging.getLogger("auth")

security = HTTPBearer()


async def get_current_admin(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Admin:
    access_token = credentials.credentials
    payload = decode_access_token(access_token)
    # Подпись верна, но токен не access (или без sub / sub не UUID) — 401, а не 500.
    if not payload or payload.get("type") != "access":
        raise HTTPException(401)
    try:
        admin_id = UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        raise HTTPException(401) from None
    admin_repo = AdminRepository(session)
    admin = await admin_repo.get_by_id(admin_id)
    if not admin or not admin.is_active:
        raise HTTPException(401)
    return admin


def require(permission: Permission) -> Callable[..., Awaitable[Admin]]:
    """Зависимость эндпоинта: текущий админ обязан иметь право permission, иначе 403.

    Роль читается из БД на каждом запросе (get_current_admin), поэтому смена роли действует сразу,
    а не после истечения access-токена.
    """

    async def dependency(admin: Annotated[Admin, Depends(get_current_admin)]) -> Admin:
        if not has_permission(admin.role, permission):
            logger.warning(
                "permission denied",
                extra={"admin_id": str(admin.id), "role": admin.role.value, "permission": permission.value},
            )
            raise HTTPException(403, detail="Insufficient permissions")
        return admin

    dependency.required_permission = permission  # type: ignore[attr-defined]  # для теста «у каждого эндпоинта есть право»
    return dependency
