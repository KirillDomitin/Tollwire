#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from modules.audit.models.audit_log import AuditAction


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    admin_id: UUID | None
    admin_email: str | None = None  # None — анонимное событие (login_failed) или админ удалён
    action: AuditAction
    entity_type: str | None
    entity_id: UUID | None
    # В ORM атрибут называется meta (metadata у моделей занято), в API — metadata, как в ТЗ.
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="meta")
    ip_address: str | None
    created_at: datetime


class AuditActorRead(BaseModel):
    id: UUID
    email: str
