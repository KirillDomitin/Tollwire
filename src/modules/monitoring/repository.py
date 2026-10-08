#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from modules.monitoring.models.metrics import ROLLUP_METRICS, ServerMetric, ServerMetricHourly


class MetricsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def add(self, metric: ServerMetric) -> None:
        self.session.add(metric)

    async def latest(self, server_id: UUID) -> ServerMetric | None:
        query = (
            select(ServerMetric).where(ServerMetric.server_id == server_id).order_by(ServerMetric.ts.desc()).limit(1)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def latest_per_server(self) -> Sequence[ServerMetric]:
        query = (
            select(ServerMetric)
            .distinct(ServerMetric.server_id)
            .order_by(ServerMetric.server_id, ServerMetric.ts.desc())
        )
        return (await self.session.execute(query)).scalars().all()

    async def raw_since(self, server_id: UUID, since: datetime) -> Sequence[ServerMetric]:
        query = (
            select(ServerMetric)
            .where(ServerMetric.server_id == server_id, ServerMetric.ts >= since)
            .order_by(ServerMetric.ts)
        )
        return (await self.session.execute(query)).scalars().all()

    async def hourly_since(self, server_id: UUID, since: datetime) -> Sequence[ServerMetricHourly]:
        query = (
            select(ServerMetricHourly)
            .where(ServerMetricHourly.server_id == server_id, ServerMetricHourly.hour >= since)
            .order_by(ServerMetricHourly.hour)
        )
        return (await self.session.execute(query)).scalars().all()

    async def rollup(self, since: datetime, until: datetime) -> int:
        """Сворачивает замеры [since, until) в часы: среднее и максимум. Повторный запуск перезаписывает те же часы
        (ON CONFLICT DO UPDATE) — свёртку можно гонять часто, пересчитывая последние часы целиком."""
        hour = func.date_trunc("hour", ServerMetric.ts)
        columns: dict[str, Any] = {
            "server_id": ServerMetric.server_id,
            "hour": hour,
            "samples": func.count(),
            "mem_total_bytes": func.max(ServerMetric.mem_total_bytes),
            "disk_total_bytes": func.max(ServerMetric.disk_total_bytes),
        }
        for name in ROLLUP_METRICS:
            column = getattr(ServerMetric, name)
            columns[f"{name}_avg"] = func.avg(column)
            columns[f"{name}_max"] = func.max(column)
        source = (
            select(*(value.label(key) for key, value in columns.items()))
            .where(ServerMetric.ts >= since, ServerMetric.ts < until)
            .group_by(ServerMetric.server_id, hour)
        )
        statement = insert(ServerMetricHourly).from_select(list(columns), source)
        statement = statement.on_conflict_do_update(
            index_elements=["server_id", "hour"],
            set_={key: statement.excluded[key] for key in columns if key not in ("server_id", "hour")},
        )
        result = cast(CursorResult[Any], await self.session.execute(statement))
        return int(result.rowcount)

    async def delete_older(self, raw_before: datetime, hourly_before: datetime) -> tuple[int, int]:
        raw = cast(
            CursorResult[Any], await self.session.execute(delete(ServerMetric).where(ServerMetric.ts < raw_before))
        )
        hourly = cast(
            CursorResult[Any],
            await self.session.execute(delete(ServerMetricHourly).where(ServerMetricHourly.hour < hourly_before)),
        )
        return int(raw.rowcount), int(hourly.rowcount)
