#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum
from uuid import UUID

from sqlalchemy import ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin, UuidPkMixin


class ProtocolType(enum.Enum):
    AMNEZIA_WG = "amnezia_wg"
    AMNEZIA_APP_AWG = "amnezia_app_awg"
    AMNEZIA_APP_AWG2 = "amnezia_app_awg2"
    # «Шлюз»: свой контейнер панели на российском сервере, завершает клиентский AWG и пересылает трафик на выходы.
    AMNEZIA_GATEWAY = "amnezia_gateway"


# Установки приложения AmneziaVPN: панель их не ставит и не удаляет, клиентов в них только отзывает и возвращает.
APP_PROTOCOLS = frozenset({ProtocolType.AMNEZIA_APP_AWG, ProtocolType.AMNEZIA_APP_AWG2})
# Протоколы, которые панель ставит и снимает сама (тело POST/DELETE /servers/{id}/protocol).
INSTALLABLE_PROTOCOLS = frozenset({ProtocolType.AMNEZIA_WG, ProtocolType.AMNEZIA_GATEWAY})


class VPNProtocol(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "protocols"
    __table_args__ = (UniqueConstraint("server_id", "type"),)
    server_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("vpn_servers.id", ondelete="CASCADE"))
    type: Mapped[ProtocolType] = mapped_column(default=ProtocolType.AMNEZIA_WG)
    port: Mapped[int | None] = mapped_column()
