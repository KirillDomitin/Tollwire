#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, UuidPkMixin


class GeoipStatus(enum.Enum):
    ACTIVE = "active"  # список, по которому работают шлюзы (не больше одного)
    CANDIDATE = "candidate"  # отличается от активного больше чем на 20%: ждёт подтверждения админа
    SUPERSEDED = "superseded"  # был активным, заменён


class OverrideAction(enum.Enum):
    DIRECT = "direct"  # сеть идёт напрямую (даже если её нет в списке)
    TUNNEL = "tunnel"  # сеть идёт через выход (даже если она в списке)


class GeoipList(UuidPkMixin, Base):
    """Список российских IPv4-префиксов (RIPE или загрузка админа). data - CIDR по строке, ≈150 КБ."""

    __tablename__ = "geoip_lists"
    __table_args__ = (
        # Не больше одного активного списка: частичный уникальный индекс по константному выражению.
        Index("uq_geoip_lists_one_active", "status", unique=True, postgresql_where=text("status = 'ACTIVE'")),
    )

    source: Mapped[str] = mapped_column(String(16))  # ripe / upload
    version: Mapped[str] = mapped_column(String(32))  # дата из файла или загрузки (YYYYMMDD)
    sha256: Mapped[str] = mapped_column(String(64))
    prefixes_count: Mapped[int] = mapped_column(Integer)
    addresses_count: Mapped[int] = mapped_column(BigInteger)
    data: Mapped[str] = mapped_column(Text)
    status: Mapped[GeoipStatus] = mapped_column(Enum(GeoipStatus, native_enum=False, length=16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class GeoipOverride(UuidPkMixin, Base):
    """Исключение администратора: сеть, которая идёт напрямую (DIRECT) или через выход (TUNNEL) вопреки списку."""

    __tablename__ = "geoip_overrides"
    __table_args__ = (UniqueConstraint("network", name="uq_geoip_overrides_network"),)

    network: Mapped[str] = mapped_column(String(18))  # IPv4 CIDR
    action: Mapped[OverrideAction] = mapped_column(Enum(OverrideAction, native_enum=False, length=16))
    note: Mapped[str | None] = mapped_column(String(100), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
