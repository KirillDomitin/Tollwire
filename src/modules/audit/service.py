#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.audit.repository import AuditLogFilter, AuditLogRepository


def _to_json(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json(item) for item in value]
    return value


def diff_changes(obj: object, new_values: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Что реально меняется: {поле: [было, стало]}. Вызывать ДО setattr. Только для несекретных полей."""
    return {field: [getattr(obj, field), value] for field, value in new_values.items() if getattr(obj, field) != value}


class AuditService:
    """Пишет журнал от имени актора. Без commit: запись фиксируется вместе с действием вызывающего.

    В meta — только несекретное: никаких паролей, ключей, токенов и конфигов.
    """

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.actor = actor
        self.session = session
        self.logs = AuditLogRepository(session)

    def record(
        self,
        action: AuditAction,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> AuditLog:
        log = AuditLog(
            admin_id=self.actor.admin_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            meta=_to_json(meta) if meta else None,
            ip_address=self.actor.ip,
        )
        self.logs.add(log)
        return log

    async def list(self, filters: AuditLogFilter, limit: int, offset: int) -> Sequence[AuditLog]:
        return await self.logs.list(filters, limit, offset)

    async def purge(self, older_than_days: int) -> int:
        """Политика хранения: удаляет записи старше N дней и коммитит (вызывает планировщик). 0 — хранить вечно."""
        if older_than_days <= 0:
            return 0
        deleted = await self.logs.delete_before(datetime.now(timezone.utc) - timedelta(days=older_than_days))
        await self.session.commit()
        return deleted
