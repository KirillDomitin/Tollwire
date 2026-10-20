#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Сверка туннелей выходов на шлюзе со строками gateway_exits: чистые функции без SSH (их вызывают sync и health)."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from infrastructure.vpn.gateway_config import ExitTunnelStatus, exit_health_label, exit_interface
from modules.gateway.models.gateway_exit import ExitState, GatewayExit


def record_exit_status(exits: Sequence[GatewayExit], statuses: Sequence[ExitTunnelStatus], now: datetime) -> None:
    """Пишет в строки выходов, что видно на шлюзе: health, последнее рукопожатие, время сверки. Состояние (state) не
    трогает: решения принимают задачи, а не проверка. Выход без туннеля (PENDING / PEER_CREATED) - `down`."""
    by_slot = {status.slot: status for status in statuses}
    for gateway_exit in exits:
        status = by_slot.get(gateway_exit.slot)
        gateway_exit.health = exit_health_label(status, now)
        gateway_exit.last_handshake_at = status.latest_handshake if status is not None else None
        gateway_exit.checked_at = now


def exit_report(
    exits: Sequence[GatewayExit], statuses: Sequence[ExitTunnelStatus]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Отчёт sync по выходам и найденные расхождения (issues). Без ключей и конфигов.

    Расхождения: в БД выход ACTIVE, а интерфейса gwx<слот> на шлюзе нет; на шлюзе есть туннель без записи в панели.
    Действий не предпринимается (failover - этап 24.5).
    """
    by_slot = {status.slot: status for status in statuses}
    report: list[dict[str, Any]] = []
    issues: list[str] = []
    for gateway_exit in exits:
        status = by_slot.get(gateway_exit.slot)
        up = status is not None and status.interface_up
        report.append(
            {
                "exit_id": str(gateway_exit.id),
                "slot": gateway_exit.slot,
                "state": gateway_exit.state.value,
                "interface_up": up,
                "health": gateway_exit.health,
            }
        )
        if gateway_exit.state == ExitState.ACTIVE and not up:
            issues.append(f"Exit {exit_interface(gateway_exit.slot)} is down/missing on the gateway")
    known = {gateway_exit.slot for gateway_exit in exits}
    for status in statuses:
        if status.interface_up and status.slot not in known:
            issues.append(f"Unknown exit tunnel {exit_interface(status.slot)} on the gateway (no record in the panel)")
    return report, issues
