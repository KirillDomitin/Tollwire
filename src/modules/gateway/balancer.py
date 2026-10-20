#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Выбор выхода для клиента шлюза: чистая функция без БД и SSH (этап 24.3)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from uuid import UUID

from modules.gateway.models.gateway_exit import ExitState

# Метки health, при которых выход не получает новых клиентов: туннель лежит или рукопожатие устарело. Пока сверки не
# было (None) или рукопожатия ещё нет (no_handshake) - выход годится: так свежеподнятый выход сразу принимает клиентов.
UNUSABLE_HEALTH = frozenset({"down", "stale"})
# Метки health, при которых проверка выхода считается плохой (failover 24.5): рукопожатия нет или оно устарело.
BAD_HEALTH = frozenset({"down", "stale", "no_handshake"})


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


# --- failover (этап 24.5) ---------------------------------------------------------------------------------------------

# Плохих проверок подряд до отказа выхода: одна плохая (блип сети, пропущенное рукопожатие) - только счётчик.
FAILURE_THRESHOLD = 2
# Закреплённых клиентов, возвращаемых на свой выход за проход (отказ срочный и переносит всех сразу, возврат - нет).
MAX_RETURNS_PER_PASS = 5


@dataclass(frozen=True)
class HealthStep:
    """Итог одной проверки выхода: новые счётчики и событие смены состояния."""

    consecutive_failures: int
    healthy_since: datetime | None
    event: str | None  # "failed" - счётчик дошёл до порога; "recovered" - первая хорошая проверка после отказа


def next_health(consecutive_failures: int, healthy_since: datetime | None, good: bool, now: datetime) -> HealthStep:
    """Гистерезис. Плохая проверка: счётчик +1, `healthy_since` сбрасывается; на пороге - событие `failed` (один раз).
    Хорошая: счётчик 0; если выход был отказавшим - событие `recovered`; `healthy_since` фиксируется при первой хорошей
    проверке после плохих (или впервые) и дальше не двигается."""
    if not good:
        failures = consecutive_failures + 1
        return HealthStep(failures, None, "failed" if failures == FAILURE_THRESHOLD else None)
    recovered = consecutive_failures >= FAILURE_THRESHOLD
    return HealthStep(
        0,
        healthy_since if healthy_since is not None and consecutive_failures == 0 else now,
        "recovered" if recovered else None,
    )


@dataclass(frozen=True)
class ExitView:
    """Выход глазами плана failover. `failed` - ACTIVE и плохих проверок подряд не меньше порога; `usable` - ACTIVE и
    ни одной плохой проверки (подходит как цель переноса); `healthy_since` - начало непрерывной исправности."""

    exit_id: UUID
    slot: int
    weight: int
    max_clients: int | None
    failed: bool
    usable: bool
    healthy_since: datetime | None


@dataclass(frozen=True)
class AssignmentView:
    credential_id: UUID
    exit_id: UUID


@dataclass(frozen=True)
class Move:
    credential_id: UUID
    from_exit: UUID
    to_exit: UUID
    reason: str  # "failover" / "failover_return"


@dataclass(frozen=True)
class FailoverPlan:
    moves: list[Move] = field(default_factory=list)
    stranded: dict[UUID, int] = field(
        default_factory=dict
    )  # exit_id отказавшего выхода -> клиентов, которым некуда деться

    @property
    def empty(self) -> bool:
        return not self.moves and not self.stranded


def plan_failover(
    exits: Sequence[ExitView],
    assignments: Sequence[AssignmentView],
    pins: Mapping[UUID, UUID],
    now: datetime,
    return_after: timedelta,
) -> FailoverPlan:
    """План переносов. Чистая детерминированная функция (порядок входа не влияет), идемпотентная: по применённому
    состоянию пуст.

    - Клиенты отказавших выходов переезжают на исправные через `pick_exit` (веса, `max_clients`), причина `failover`,
      все затронутые - за один проход. Нет исправных - клиент остаётся (kill-switch), выход попадает в `stranded`.
    - Незакреплённый клиент остаётся там, куда переехал: автоматического возврата нет.
    - Закреплённый (`pins`: credential -> выход) уезжает так же; когда его выход непрерывно исправен не меньше
      `return_after`, клиент возвращается (причина `failover_return`), не больше `MAX_RETURNS_PER_PASS` за проход.
    """
    by_id = {view.exit_id: view for view in exits}
    counts: dict[UUID, int] = {}
    for assignment in assignments:
        counts[assignment.exit_id] = counts.get(assignment.exit_id, 0) + 1

    def candidates() -> list[ExitCandidate]:
        return [
            ExitCandidate(
                view.exit_id,
                view.slot,
                view.weight,
                view.max_clients,
                counts.get(view.exit_id, 0),
                ExitState.ACTIVE,
                None if view.usable else "down",
            )
            for view in exits
        ]

    def returnable(pinned: UUID | None) -> bool:
        view = by_id.get(pinned) if pinned is not None else None
        return (
            view is not None
            and view.usable
            and view.healthy_since is not None
            and now - view.healthy_since >= return_after
            and (view.max_clients is None or counts.get(view.exit_id, 0) < view.max_clients)
        )

    moves: list[Move] = []
    stranded: dict[UUID, int] = {}
    returns = 0
    for assignment in sorted(assignments, key=lambda item: str(item.credential_id)):
        current = by_id.get(assignment.exit_id)
        if current is None:
            continue
        pinned = pins.get(assignment.credential_id)
        if current.failed:
            if returns < MAX_RETURNS_PER_PASS and pinned != current.exit_id and returnable(pinned):
                target, reason = by_id[pinned], "failover_return"  # type: ignore[index]
                returns += 1
            else:
                chosen = pick_exit(candidates())
                if chosen is None:
                    stranded[current.exit_id] = stranded.get(current.exit_id, 0) + 1
                    continue
                target, reason = by_id[chosen.exit_id], "failover"
        elif pinned is not None and pinned != current.exit_id and returns < MAX_RETURNS_PER_PASS and returnable(pinned):
            target, reason = by_id[pinned], "failover_return"
            returns += 1
        else:
            continue
        counts[current.exit_id] = counts.get(current.exit_id, 0) - 1
        counts[target.exit_id] = counts.get(target.exit_id, 0) + 1
        moves.append(Move(assignment.credential_id, current.exit_id, target.exit_id, reason))
    return FailoverPlan(moves, stranded)
