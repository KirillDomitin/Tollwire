#!/usr/bin/python
# -*- coding: utf-8 -*-
from dataclasses import dataclass, replace
from uuid import UUID


@dataclass(frozen=True)
class AuditActor:
    """Кто совершает действие: админ (или None — система) и IP, с которого пришёл запрос."""

    admin_id: UUID | None
    ip: str | None = None

    def as_admin(self, admin_id: UUID) -> "AuditActor":
        return replace(self, admin_id=admin_id)


# Автоматические действия (истечение срока, внутренние чтения) — без админа и IP.
SYSTEM_ACTOR = AuditActor(admin_id=None)
