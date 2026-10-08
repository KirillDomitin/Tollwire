#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class MetricsRange(str, Enum):
    """Период графика. До суток — поминутные замеры, дальше — почасовая свёртка (не больше ~1500 точек)."""

    HOUR = "1h"
    DAY = "24h"
    WEEK = "7d"
    MONTH = "30d"


class MetricPoint(BaseModel):
    """Точка графика. Для почасовых данных значения — средние за час, а *_max — пик внутри часа."""

    ts: datetime
    cpu_percent: float | None
    load1: float | None
    mem_used_bytes: float | None
    mem_total_bytes: float | None
    disk_used_bytes: float | None
    disk_total_bytes: float | None
    net_rx_bytes_per_s: float | None
    net_tx_bytes_per_s: float | None
    disk_read_bytes_per_s: float | None
    disk_write_bytes_per_s: float | None
    clients_total: float | None
    clients_online: float | None
    cpu_percent_max: float | None = None
    net_rx_bytes_per_s_max: float | None = None
    net_tx_bytes_per_s_max: float | None = None
    clients_online_max: float | None = None


class MetricsSeries(BaseModel):
    range: MetricsRange
    resolution: str  # "minute" | "hour"
    points: list[MetricPoint]


class LatestMetrics(BaseModel):
    """Последний замер сервера — для списка серверов."""

    server_id: UUID
    ts: datetime
    cpu_percent: float | None
    mem_used_bytes: int
    mem_total_bytes: int
    disk_used_bytes: int
    disk_total_bytes: int
    net_rx_bytes_per_s: float | None
    net_tx_bytes_per_s: float | None
    clients_total: int | None
    clients_online: int | None
