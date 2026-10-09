#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.auth.models.admin import Admin


@dataclass
class AuditLogFilter:
    admin_id: UUID | None = None
    actions: Sequence[AuditAction] | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    since: datetime | None = None
    until: datetime | None = None
    # Курсор: строки строго «старше» (created_at, id) — стабильно при одинаковом created_at и новых записях.
    before: tuple[datetime, UUID] | None = None


class AuditLogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def add(self, log: AuditLog) -> None:
        # Без flush: запись уходит в БД тем же commit'ом, что и само действие.
        self.session.add(log)

    async def list(self, filters: AuditLogFilter, limit: int, offset: int) -> Sequence[tuple[AuditLog, str | None]]:
        """Записи вместе с email админа (None — анонимное событие или админ удалён)."""
        query = select(AuditLog, Admin.email).outerjoin(Admin, Admin.id == AuditLog.admin_id)
        if filters.admin_id:
            query = query.where(AuditLog.admin_id == filters.admin_id)
        if filters.actions:
            query = query.where(AuditLog.action.in_(filters.actions))
        if filters.entity_type:
            query = query.where(AuditLog.entity_type == filters.entity_type)
        if filters.entity_id:
            query = query.where(AuditLog.entity_id == filters.entity_id)
        if filters.since:
            query = query.where(AuditLog.created_at >= filters.since)
        if filters.until:
            query = query.where(AuditLog.created_at < filters.until)
        if filters.before:
            created_at, log_id = filters.before
            query = query.where(tuple_(AuditLog.created_at, AuditLog.id) < tuple_(literal(created_at), literal(log_id)))
        query = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return [(log, email) for log, email in result.all()]

    async def list_actors(self) -> Sequence[tuple[UUID, str]]:
        """Админы, которые есть в журнале, — для фильтра «Кто»."""
        query = (
            select(Admin.id, Admin.email)
            .where(Admin.id.in_(select(AuditLog.admin_id).where(AuditLog.admin_id.is_not(None)).distinct()))
            .order_by(Admin.email)
        )
        result = await self.session.execute(query)
        return [(admin_id, email) for admin_id, email in result.all()]

    async def delete_before(self, before: datetime) -> int:
        query = delete(AuditLog).where(AuditLog.created_at < before)
        result = cast(CursorResult[Any], await self.session.execute(query))
        return int(result.rowcount)
