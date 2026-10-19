#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from modules.protocols.models.protocol import INSTALLABLE_PROTOCOLS, ProtocolType


class ProtocolRequest(BaseModel):
    """Необязательное тело POST/DELETE /servers/{id}/protocol. Без тела - обычный протокол панели (AMNEZIA_WG)."""

    type: ProtocolType = ProtocolType.AMNEZIA_WG

    @field_validator("type")
    @classmethod
    def _installable(cls, value: ProtocolType) -> ProtocolType:
        if value not in INSTALLABLE_PROTOCOLS:
            raise ValueError("Only protocols installed by the panel can be installed or removed")
        return value


class ProtocolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    server_id: UUID
    type: ProtocolType
    port: int | None
    created_at: datetime
