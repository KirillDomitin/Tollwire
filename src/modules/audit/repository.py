#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit.models.audit_log import AuditAction, AuditLog


@dataclass
class AuditLogFilter:
    admin_id: UUID | None = None
    action: AuditAction | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    since: datetime | None = None
    until: datetime | None = None


class AuditLogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def add(self, log: AuditLog) -> None:
        # Без flush: запись уходит в БД тем же commit'ом, что и само действие.
        self.session.add(log)

    async def list(self, filters: AuditLogFilter, limit: int, offset: int) -> Sequence[AuditLog]:
        query = select(AuditLog)
        if filters.admin_id:
            query = query.where(AuditLog.admin_id == filters.admin_id)
        if filters.action:
            query = query.where(AuditLog.action == filters.action)
        if filters.entity_type:
            query = query.where(AuditLog.entity_type == filters.entity_type)
        if filters.entity_id:
            query = query.where(AuditLog.entity_id == filters.entity_id)
        if filters.since:
            query = query.where(AuditLog.created_at >= filters.since)
        if filters.until:
            query = query.where(AuditLog.created_at < filters.until)
        query = query.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return result.scalars().all()
