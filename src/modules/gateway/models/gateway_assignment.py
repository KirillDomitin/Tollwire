#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class AssignReason(enum.Enum):
    ISSUE = "issue"  # клиенту выдан конфиг, выход выбран при выдаче (или позже, когда появился исправный выход)
    DRAIN = "drain"  # выход снимают, клиент перенесён на другой
    FAILOVER = "failover"  # выход вышел из строя, клиент перенесён (этап 24.5)
    MANUAL = "manual"  # назначил админ
    SYNC = "sync"  # назначен сверкой шлюза


class GatewayAssignment(Base):
    """Выход клиента шлюза: на какой выход идёт трафик клиента. Строка есть - у клиента есть выход; нет - клиент без
    интернета (kill-switch). Источник правды о картe на шлюзе: файл assign.rules собирается из этой таблицы целиком."""

    __tablename__ = "gateway_assignments"

    # Клиент отозван (credential удалён) - назначение уходит с ним. Живой credential один на грант: ключ - он сам.
    credential_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("vpn_credentials.id", ondelete="CASCADE"), primary_key=True
    )
    exit_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("gateway_exits.id", ondelete="CASCADE"), index=True)
    # Закреплённый выход (задел под этап 24.5): выход удалили - закрепление снимается.
    pinned_exit_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("gateway_exits.id", ondelete="SET NULL"), default=None
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reason: Mapped[AssignReason] = mapped_column(
        Enum(AssignReason, native_enum=False, length=16), default=AssignReason.ISSUE, server_default=text("'ISSUE'")
    )
