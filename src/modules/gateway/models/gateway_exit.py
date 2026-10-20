#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin

# Слот - номер выхода в пределах шлюза: из него строятся интерфейс gwx<слот> и таблица маршрутов (gateway_config.py).
MAX_SLOT = 200
MAX_WEIGHT = 100


class ExitState(enum.Enum):
    PENDING = "pending"  # строка создана, на выходе ещё ничего нет
    PEER_CREATED = "peer_created"  # внутренний клиент создан на выходе, конфиг сохранён; туннеля на шлюзе ещё нет
    ACTIVE = "active"  # туннель на шлюзе поднят
    DRAINING = "draining"  # выход снимают: новые клиенты не назначаются, существующие переносятся (этап 24.3)
    REMOVING = "removing"  # переносить некого: снимается туннель на шлюзе и клиент на выходе


class GatewayExit(UuidPkMixin, TimestampMixin, Base):
    """Выход шлюза: зарубежный сервер панели, к которому шлюз подключён как клиент (интерфейс gwx<slot> на шлюзе).

    Шлюз - это протокол AMNEZIA_GATEWAY на сервере-шлюзе, выход - протокол AMNEZIA_WG на сервере-выходе. На выходе
    панель создаёт «внутреннего клиента» (`client_*`), его конфиг хранится зашифрованным: по нему туннель поднимается
    на шлюзе, а sync возвращает потерянного клиента на выход.
    """

    __tablename__ = "gateway_exits"
    __table_args__ = (
        UniqueConstraint("gateway_protocol_id", "slot", name="uq_gateway_exits_slot"),
        UniqueConstraint("gateway_protocol_id", "exit_protocol_id", name="uq_gateway_exits_exit"),
        CheckConstraint(f"slot BETWEEN 1 AND {MAX_SLOT}", name="ck_gateway_exits_slot"),
        CheckConstraint(f"weight BETWEEN 1 AND {MAX_WEIGHT}", name="ck_gateway_exits_weight"),
        CheckConstraint("max_clients IS NULL OR max_clients > 0", name="ck_gateway_exits_max_clients"),
    )

    # Шлюз снимают - выходы уходят с ним (внутренние клиенты на выходах убирает sync выходов).
    gateway_protocol_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("protocols.id", ondelete="CASCADE"))
    # RESTRICT: пока выход подключён к шлюзу, его протокол (и сервер) не удалить - сначала снять выход.
    exit_protocol_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("protocols.id", ondelete="RESTRICT"), index=True)
    slot: Mapped[int] = mapped_column(SmallInteger)
    weight: Mapped[int] = mapped_column(SmallInteger, default=1, server_default=text("1"))
    max_clients: Mapped[int | None] = mapped_column(Integer, default=None)
    # VARCHAR, а не Postgres-enum: новые состояния добавляются без ALTER TYPE (как AuditAction).
    state: Mapped[ExitState] = mapped_column(
        Enum(ExitState, native_enum=False, length=16), default=ExitState.PENDING, server_default=text("'PENDING'")
    )
    # ok / stale / no_handshake / down - по последней сверке туннеля (exit_health_label); NULL - ещё не сверяли.
    health: Mapped[str | None] = mapped_column(String(16), default=None)
    last_handshake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Failover (24.5): подряд плохих проверок (рукопожатие устарело / нет туннеля / сервер-выход DOWN); отказавшим выход
    # считается при FAILURE_THRESHOLD плохих подряд. healthy_since - с какого момента проверки непрерывно хорошие.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    healthy_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Событие `gateway_failover` за этот отказ уже записано; сбрасывается при восстановлении.
    failure_notified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # Внутренний клиент на выходе. Публичный ключ и адрес - не секрет; конфиг (приватный ключ) - только Fernet.
    client_public_key: Mapped[str | None] = mapped_column(String(64), default=None)
    client_address: Mapped[str | None] = mapped_column(String(64), default=None)
    encrypted_config: Mapped[str | None] = mapped_column(Text, default=None)
    # Итог переноса клиентов при снятии выхода (24.3): сколько перенесено на другие выходы и сколько осталось без.
    # Копятся по повторам (перенос коммитится вместе с применением карты), поэтому счётчики аудита не теряются.
    drained_moved: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    drained_unassigned: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
