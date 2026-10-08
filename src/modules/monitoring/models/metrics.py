#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Метрики серверов: поминутные замеры (хранятся неделю) и почасовая свёртка (среднее и пик, 90 дней)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base

# Метрики, у которых в почасовой свёртке хранятся среднее и максимум (колонки <имя>_avg, <имя>_max).
ROLLUP_METRICS = (
    "cpu_percent",
    "load1",
    "mem_used_bytes",
    "disk_used_bytes",
    "net_rx_bytes_per_s",
    "net_tx_bytes_per_s",
    "disk_read_bytes_per_s",
    "disk_write_bytes_per_s",
    "clients_total",
    "clients_online",
)


class ServerMetric(Base):
    """Один замер. Ключ — (сервер, время): замеров на сервер много, отдельный UUID не нужен."""

    __tablename__ = "server_metrics"
    server_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_servers.id", ondelete="CASCADE"), primary_key=True)
    # Индекс по времени — для чистки старых замеров сразу по всем серверам (ключ начинается с server_id).
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, index=True)

    # Накопительные счётчики сервера — из них со следующим замером считаются скорости.
    uptime_seconds: Mapped[float] = mapped_column(Float)
    cpu_total: Mapped[int] = mapped_column(BigInteger)
    cpu_idle: Mapped[int] = mapped_column(BigInteger)
    net_rx_bytes: Mapped[int] = mapped_column(BigInteger)
    net_tx_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_read_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_written_bytes: Mapped[int] = mapped_column(BigInteger)

    # Мгновенные значения.
    load1: Mapped[float] = mapped_column(Float)
    mem_total_bytes: Mapped[int] = mapped_column(BigInteger)
    mem_used_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_used_bytes: Mapped[int] = mapped_column(BigInteger)

    # Скорости с прошлого замера. None — первый замер или сервер перезагрузился между замерами.
    cpu_percent: Mapped[float | None] = mapped_column(Float, default=None)
    net_rx_bytes_per_s: Mapped[float | None] = mapped_column(Float, default=None)
    net_tx_bytes_per_s: Mapped[float | None] = mapped_column(Float, default=None)
    disk_read_bytes_per_s: Mapped[float | None] = mapped_column(Float, default=None)
    disk_write_bytes_per_s: Mapped[float | None] = mapped_column(Float, default=None)

    # Клиенты VPN на сервере. None — протокол не установлен или его опрос не удался.
    clients_total: Mapped[int | None] = mapped_column(Integer, default=None)
    clients_online: Mapped[int | None] = mapped_column(Integer, default=None)


class ServerMetricHourly(Base):
    """Свёртка за час: среднее и пик. Для графиков за неделю и месяц — 168 и 720 точек вместо десятков тысяч."""

    __tablename__ = "server_metrics_hourly"
    server_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_servers.id", ondelete="CASCADE"), primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, index=True)
    samples: Mapped[int] = mapped_column(Integer)
    mem_total_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger)

    cpu_percent_avg: Mapped[float | None] = mapped_column(Float, default=None)
    cpu_percent_max: Mapped[float | None] = mapped_column(Float, default=None)
    load1_avg: Mapped[float | None] = mapped_column(Float, default=None)
    load1_max: Mapped[float | None] = mapped_column(Float, default=None)
    mem_used_bytes_avg: Mapped[float | None] = mapped_column(Float, default=None)
    mem_used_bytes_max: Mapped[float | None] = mapped_column(Float, default=None)
    disk_used_bytes_avg: Mapped[float | None] = mapped_column(Float, default=None)
    disk_used_bytes_max: Mapped[float | None] = mapped_column(Float, default=None)
    net_rx_bytes_per_s_avg: Mapped[float | None] = mapped_column(Float, default=None)
    net_rx_bytes_per_s_max: Mapped[float | None] = mapped_column(Float, default=None)
    net_tx_bytes_per_s_avg: Mapped[float | None] = mapped_column(Float, default=None)
    net_tx_bytes_per_s_max: Mapped[float | None] = mapped_column(Float, default=None)
    disk_read_bytes_per_s_avg: Mapped[float | None] = mapped_column(Float, default=None)
    disk_read_bytes_per_s_max: Mapped[float | None] = mapped_column(Float, default=None)
    disk_write_bytes_per_s_avg: Mapped[float | None] = mapped_column(Float, default=None)
    disk_write_bytes_per_s_max: Mapped[float | None] = mapped_column(Float, default=None)
    clients_total_avg: Mapped[float | None] = mapped_column(Float, default=None)
    clients_total_max: Mapped[float | None] = mapped_column(Float, default=None)
    clients_online_avg: Mapped[float | None] = mapped_column(Float, default=None)
    clients_online_max: Mapped[float | None] = mapped_column(Float, default=None)
