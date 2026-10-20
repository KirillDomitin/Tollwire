#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from modules.geoip.models.geoip import GeoipStatus, OverrideAction


class GeoipListRead(BaseModel):
    """Список без самих префиксов (≈150 КБ): версия, размер, отпечаток и состояние."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    version: str
    sha256: str
    prefixes_count: int
    addresses_count: int
    status: GeoipStatus
    created_at: datetime
    activated_at: datetime | None


class GeoipOverview(BaseModel):
    active: GeoipListRead | None
    candidates: list[GeoipListRead]


class OverrideCreate(BaseModel):
    network: str = Field(max_length=18)  # IPv4 CIDR; формат и безопасность проверяет сервис (422)
    action: OverrideAction
    note: str | None = Field(default=None, max_length=100)


class OverrideRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    network: str
    action: OverrideAction
    note: str | None
    created_at: datetime


class GeoipIngestResult(BaseModel):
    """Итог получения списка: принят и активен, принят кандидатом (нужно подтверждение) или совпал с активным."""

    status: str  # active / candidate / unchanged
    list_id: UUID
    version: str
    prefixes: int
    sha256: str
    skipped: int
    rejected: int = 0  # сетей отброшено как непубличные / зарезервированные / крупнее /12
