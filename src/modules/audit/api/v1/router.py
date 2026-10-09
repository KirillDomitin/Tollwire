#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.audit.models.audit_log import AuditAction
from modules.audit.repository import AuditLogFilter
from modules.audit.schemas import AuditActorRead, AuditLogRead
from modules.audit.service import AuditService
from modules.auth.dependencies import require
from modules.auth.permissions import Permission

# Только чтение: журнал нельзя изменить или удалить через API.
router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit"])


def get_audit_service(
    session: Annotated[AsyncSession, Depends(get_session)], actor: Annotated[AuditActor, Depends(get_audit_actor)]
) -> AuditService:
    return AuditService(session, actor)


@router.get("", dependencies=[Depends(require(Permission.AUDIT_READ))], response_model=list[AuditLogRead])
async def list_audit_logs(
    service: AuditService = Depends(get_audit_service),
    admin_id: UUID | None = None,
    action: Annotated[list[AuditAction] | None, Query()] = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_created_at: datetime | None = None,
    before_id: UUID | None = None,
    limit: int = Query(default=50, le=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> list[AuditLogRead]:
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="before_created_at and before_id go together")
    filters = AuditLogFilter(
        admin_id=admin_id,
        actions=action,
        entity_type=entity_type,
        entity_id=entity_id,
        since=since,
        until=until,
        before=(before_created_at, before_id) if before_created_at and before_id else None,
    )
    rows = await service.list(filters, limit, offset)
    return [AuditLogRead.model_validate(log).model_copy(update={"admin_email": email}) for log, email in rows]


@router.get("/actors", dependencies=[Depends(require(Permission.AUDIT_READ))], response_model=list[AuditActorRead])
async def list_audit_actors(service: AuditService = Depends(get_audit_service)) -> list[AuditActorRead]:
    return [AuditActorRead(id=admin_id, email=email) for admin_id, email in await service.list_actors()]
