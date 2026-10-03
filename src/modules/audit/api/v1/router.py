#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.audit.repository import AuditLogFilter
from modules.audit.schemas import AuditLogRead
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
    action: AuditAction | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, le=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> Sequence[AuditLog]:
    filters = AuditLogFilter(
        admin_id=admin_id, action=action, entity_type=entity_type, entity_id=entity_id, since=since, until=until
    )
    return await service.list(filters, limit, offset)
