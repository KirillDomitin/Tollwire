#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Выбор выхода для клиента шлюза: чистая функция без БД и SSH (этап 24.3)."""

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from uuid import UUID

from modules.gateway.models.gateway_exit import ExitState

# Метки health, при которых выход не получает новых клиентов: туннель лежит или рукопожатие устарело. Пока сверки не
# было (None) или рукопожатия ещё нет (no_handshake) - выход годится: так свежеподнятый выход сразу принимает клиентов.
UNUSABLE_HEALTH = frozenset({"down", "stale"})


@dataclass(frozen=True)
class ExitCandidate:
    exit_id: UUID
    slot: int
    weight: int
    max_clients: int | None
    assigned: int  # сколько клиентов уже назначено на этот выход
    state: ExitState
    health: str | None


def is_usable(candidate: ExitCandidate) -> bool:
    """Выход принимает новых клиентов: ACTIVE, health не down/stale, предел max_clients не достигнут."""
    if candidate.state != ExitState.ACTIVE or candidate.health in UNUSABLE_HEALTH:
        return False
    return candidate.max_clients is None or candidate.assigned < candidate.max_clients


def pick_exit(candidates: Sequence[ExitCandidate]) -> ExitCandidate | None:
    """Выход с наименьшей нагрузкой `(assigned + 1) / weight`; при равной - меньший слот. Нет подходящих - None.
    Детерминированно: порядок кандидатов не влияет на результат; дробь точная (Fraction), без округлений."""
    usable = [candidate for candidate in candidates if is_usable(candidate)]
    if not usable:
        return None
    return min(usable, key=lambda candidate: (Fraction(candidate.assigned + 1, candidate.weight), candidate.slot))
