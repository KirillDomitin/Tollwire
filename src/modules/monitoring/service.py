#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.postgres import session_factory
from core.exceptions import CredentialNotFound, NotFoundError, SSHError
from core.log import bind
from infrastructure.monitoring.host_metrics import HostCounters, compute_rates, read_host_sample
from modules.monitoring.models.metrics import ServerMetric, ServerMetricHourly
from modules.monitoring.repository import MetricsRepository
from modules.monitoring.schemas import LatestMetrics, MetricPoint, MetricsRange, MetricsSeries
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import ServerRepository
from modules.servers.service import ServerConnectionService, provider_class

logger = logging.getLogger("monitoring")


def _counters(metric: ServerMetric) -> HostCounters:
    return HostCounters(
        uptime_seconds=metric.uptime_seconds,
        cpu_total=metric.cpu_total,
        cpu_idle=metric.cpu_idle,
        net_rx_bytes=metric.net_rx_bytes,
        net_tx_bytes=metric.net_tx_bytes,
        disk_read_bytes=metric.disk_read_bytes,
        disk_written_bytes=metric.disk_written_bytes,
    )


class MetricsCollector:
    """Замеры серверов (вызывает планировщик). На серверах только чтение; статусы серверов не меняет —
    это дело health и sync."""

    async def collect_all(self) -> str:
        async with session_factory() as session:
            server_ids = await ServerRepository(session).list_checkable_ids(with_protocol=False)
        limit = asyncio.Semaphore(max(settings.metrics_concurrency, 1))

        async def guarded(server_id: UUID) -> bool:
            async with limit:
                return await self.collect(server_id)

        results = await asyncio.gather(*(guarded(server_id) for server_id in server_ids))
        return f"metrics collected from {sum(results)}/{len(server_ids)} servers"

    async def collect(self, server_id: UUID) -> bool:
        """Один замер одного сервера. Сбой — пропуск точки (дырка на графике), а не ошибка всего правила."""
        with bind(server_id=str(server_id)):
            try:
                await self._collect(server_id)
            except (SSHError, ValueError, NotFoundError, CredentialNotFound) as exc:
                logger.warning("metrics sample failed", extra={"error": type(exc).__name__})
                return False
            return True

    async def _collect(self, server_id: UUID) -> None:
        async with session_factory() as session:
            ssh_client = await ServerConnectionService(session).create_ssh_client(server_id)
            protocols = await ProtocolRepository(session).list_by_server_id(server_id)
            protocol_types = [protocol.type for protocol in protocols]  # обычный протокол и шлюз бывают рядом
            previous = await MetricsRepository(session).latest(server_id)
            previous_counters = _counters(previous) if previous else None
            # Всё прочитано — соединение с БД обратно в пул до похода по сети. После отката объекты из БД
            # «протухают», поэтому всё нужное (тип протокола, счётчики) уже переложено в обычные значения.
            await session.rollback()

        now = datetime.now(timezone.utc)
        clients: tuple[int, int] | None = None
        async with ssh_client:
            sample = await read_host_sample(ssh_client)
            if protocol_types:
                counts = [await provider_class(item)(ssh_client).client_counts(now) for item in protocol_types]
                clients = (sum(total for total, _ in counts), sum(online for _, online in counts))

        rates = compute_rates(previous_counters, sample.counters) if previous_counters else None
        counters = sample.counters
        metric = ServerMetric(
            server_id=server_id,
            ts=now,
            uptime_seconds=counters.uptime_seconds,
            cpu_total=counters.cpu_total,
            cpu_idle=counters.cpu_idle,
            net_rx_bytes=counters.net_rx_bytes,
            net_tx_bytes=counters.net_tx_bytes,
            disk_read_bytes=counters.disk_read_bytes,
            disk_written_bytes=counters.disk_written_bytes,
            load1=sample.load1,
            mem_total_bytes=sample.mem_total_bytes,
            mem_used_bytes=sample.mem_used_bytes,
            disk_total_bytes=sample.disk_total_bytes,
            disk_used_bytes=sample.disk_used_bytes,
            cpu_percent=rates.cpu_percent if rates else None,
            net_rx_bytes_per_s=rates.net_rx_bytes_per_s if rates else None,
            net_tx_bytes_per_s=rates.net_tx_bytes_per_s if rates else None,
            disk_read_bytes_per_s=rates.disk_read_bytes_per_s if rates else None,
            disk_write_bytes_per_s=rates.disk_write_bytes_per_s if rates else None,
            clients_total=clients[0] if clients else None,
            clients_online=clients[1] if clients else None,
        )
        async with session_factory() as session:
            MetricsRepository(session).add(metric)
            await session.commit()


_WINDOWS = {
    MetricsRange.HOUR: timedelta(hours=1),
    MetricsRange.DAY: timedelta(days=1),
    MetricsRange.WEEK: timedelta(days=7),
    MetricsRange.MONTH: timedelta(days=30),
}
_MINUTE_RANGES = {MetricsRange.HOUR, MetricsRange.DAY}


def _minute_point(row: ServerMetric) -> MetricPoint:
    return MetricPoint(
        ts=row.ts,
        cpu_percent=row.cpu_percent,
        load1=row.load1,
        mem_used_bytes=row.mem_used_bytes,
        mem_total_bytes=row.mem_total_bytes,
        disk_used_bytes=row.disk_used_bytes,
        disk_total_bytes=row.disk_total_bytes,
        net_rx_bytes_per_s=row.net_rx_bytes_per_s,
        net_tx_bytes_per_s=row.net_tx_bytes_per_s,
        disk_read_bytes_per_s=row.disk_read_bytes_per_s,
        disk_write_bytes_per_s=row.disk_write_bytes_per_s,
        clients_total=row.clients_total,
        clients_online=row.clients_online,
    )


def _hour_point(row: ServerMetricHourly) -> MetricPoint:
    return MetricPoint(
        ts=row.hour,
        cpu_percent=row.cpu_percent_avg,
        load1=row.load1_avg,
        mem_used_bytes=row.mem_used_bytes_avg,
        mem_total_bytes=row.mem_total_bytes,
        disk_used_bytes=row.disk_used_bytes_avg,
        disk_total_bytes=row.disk_total_bytes,
        net_rx_bytes_per_s=row.net_rx_bytes_per_s_avg,
        net_tx_bytes_per_s=row.net_tx_bytes_per_s_avg,
        disk_read_bytes_per_s=row.disk_read_bytes_per_s_avg,
        disk_write_bytes_per_s=row.disk_write_bytes_per_s_avg,
        clients_total=row.clients_total_avg,
        clients_online=row.clients_online_avg,
        cpu_percent_max=row.cpu_percent_max,
        net_rx_bytes_per_s_max=row.net_rx_bytes_per_s_max,
        net_tx_bytes_per_s_max=row.net_tx_bytes_per_s_max,
        clients_online_max=row.clients_online_max,
    )


class MetricsService:
    """Чтение метрик для API."""

    def __init__(self, session: AsyncSession):
        self.metrics = MetricsRepository(session)
        self.servers = ServerRepository(session)

    async def series(self, server_id: UUID, metrics_range: MetricsRange) -> MetricsSeries:
        if not await self.servers.get_by_id(server_id):
            raise NotFoundError(f"Server '{server_id}' not found")
        since = datetime.now(timezone.utc) - _WINDOWS[metrics_range]
        if metrics_range in _MINUTE_RANGES:
            points = [_minute_point(row) for row in await self.metrics.raw_since(server_id, since)]
            return MetricsSeries(range=metrics_range, resolution="minute", points=points)
        hour_start = since.replace(minute=0, second=0, microsecond=0)
        points = [_hour_point(row) for row in await self.metrics.hourly_since(server_id, hour_start)]
        return MetricsSeries(range=metrics_range, resolution="hour", points=points)

    async def latest(self) -> list[LatestMetrics]:
        return [
            LatestMetrics(
                server_id=row.server_id,
                ts=row.ts,
                cpu_percent=row.cpu_percent,
                mem_used_bytes=row.mem_used_bytes,
                mem_total_bytes=row.mem_total_bytes,
                disk_used_bytes=row.disk_used_bytes,
                disk_total_bytes=row.disk_total_bytes,
                net_rx_bytes_per_s=row.net_rx_bytes_per_s,
                net_tx_bytes_per_s=row.net_tx_bytes_per_s,
                clients_total=row.clients_total,
                clients_online=row.clients_online,
            )
            for row in await self.metrics.latest_per_server()
        ]


async def rollup_and_prune(now: datetime | None = None) -> str:
    """Свёртка последних часов (повторная — перезаписывает) и удаление истории старше сроков хранения."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    async with session_factory() as session:
        repository = MetricsRepository(session)
        hours = await repository.rollup(since, now)
        raw, hourly = await repository.delete_older(
            now - timedelta(days=settings.metrics_raw_retention_days),
            now - timedelta(days=settings.metrics_hourly_retention_days),
        )
        await session.commit()
    return f"rolled up {hours} server-hours, pruned {raw} samples and {hourly} hourly rows"
