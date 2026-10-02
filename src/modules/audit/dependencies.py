#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Annotated

from fastapi import Depends, Request

from modules.audit.actor import AuditActor
from modules.auth.dependencies import get_current_admin
from modules.auth.models.admin import Admin


def get_request_ip(request: Request) -> str | None:
    # TODO(блок 17): за nginx здесь будет адрес прокси — брать X-Forwarded-For только от доверенного прокси.
    return request.client.host if request.client else None


def get_anonymous_actor(request: Request) -> AuditActor:
    """Для эндпоинтов без авторизации (login): админ ещё неизвестен."""
    return AuditActor(admin_id=None, ip=get_request_ip(request))


async def get_audit_actor(request: Request, admin: Annotated[Admin, Depends(get_current_admin)]) -> AuditActor:
    return AuditActor(admin_id=admin.id, ip=get_request_ip(request))
