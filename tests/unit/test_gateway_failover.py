"""Failover выходов шлюза (этап 24.5): гистерезис проверок и чистый план переносов `plan_failover`."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from modules.gateway.balancer import (
    FAILURE_THRESHOLD,
    MAX_RETURNS_PER_PASS,
    AssignmentView,
    ExitView,
    FailoverPlan,
    next_health,
    plan_failover,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
RETURN_AFTER = timedelta(minutes=10)
LONG_AGO = NOW - timedelta(hours=1)


def exit_(
    slot: int,
    *,
    failed: bool = False,
    usable: bool | None = None,
    weight: int = 1,
    max_clients: int | None = None,
    healthy_since: datetime | None = LONG_AGO,
) -> ExitView:
    return ExitView(
        UUID(int=slot),
        slot,
        weight,
        max_clients,
        failed,
        (not failed) if usable is None else usable,
        None if failed else healthy_since,
    )


def client(number: int, on: ExitView) -> AssignmentView:
    return AssignmentView(UUID(int=1000 + number), on.exit_id)


def targets(plan: FailoverPlan) -> list[tuple[int, int, str]]:
    """(номер клиента, слот-цель, причина) по порядку; слот восстанавливается из UUID(int=slot)."""
    return [(move.credential_id.int - 1000, move.to_exit.int, move.reason) for move in plan.moves]


def apply(plan: FailoverPlan, assignments: list[AssignmentView]) -> list[AssignmentView]:
    moved = {move.credential_id: move.to_exit for move in plan.moves}
    return [AssignmentView(item.credential_id, moved.get(item.credential_id, item.exit_id)) for item in assignments]


# --- гистерезис -----------------------------------------------------------------------------------------------------


def test_one_bad_check_is_only_a_counter_and_two_are_a_failure() -> None:
    first = next_health(0, NOW, False, NOW)
    assert (first.consecutive_failures, first.healthy_since, first.event) == (1, None, None)
    second = next_health(first.consecutive_failures, first.healthy_since, False, NOW)
    assert (second.consecutive_failures, second.event) == (FAILURE_THRESHOLD, "failed")
    third = next_health(second.consecutive_failures, None, False, NOW)
    assert (third.consecutive_failures, third.event) == (3, None)  # событие один раз, на пороге


def test_the_first_good_check_after_a_failure_recovers_and_fixes_the_healthy_since() -> None:
    step = next_health(3, None, True, NOW)
    assert (step.consecutive_failures, step.healthy_since, step.event) == (0, NOW, "recovered")


def test_a_good_check_after_one_bad_is_not_a_recovery_but_restarts_the_healthy_streak() -> None:
    step = next_health(1, None, True, NOW)
    assert (step.consecutive_failures, step.healthy_since, step.event) == (0, NOW, None)


def test_the_healthy_since_does_not_move_while_the_exit_stays_healthy() -> None:
    step = next_health(0, LONG_AGO, True, NOW)
    assert (step.healthy_since, step.event) == (LONG_AGO, None)
    assert next_health(0, None, True, NOW).healthy_since == NOW  # первая проверка вообще


def test_a_bad_check_resets_the_healthy_since() -> None:
    assert next_health(0, LONG_AGO, False, NOW).healthy_since is None


# --- план: отказ ------------------------------------------------------------------------------------------------------


def test_nothing_to_do_when_every_exit_is_healthy() -> None:
    a, b = exit_(1), exit_(2)
    plan = plan_failover([a, b], [client(1, a), client(2, b)], {}, NOW, RETURN_AFTER)
    assert plan.empty and plan.moves == [] and plan.stranded == {}


def test_clients_of_a_failed_exit_move_to_the_healthy_one() -> None:
    a, b = exit_(1, failed=True), exit_(2)
    plan = plan_failover([a, b], [client(1, a), client(2, a), client(3, b)], {}, NOW, RETURN_AFTER)
    assert targets(plan) == [(1, 2, "failover"), (2, 2, "failover")] and plan.stranded == {}


def test_all_affected_clients_move_in_one_pass_even_when_there_are_many() -> None:
    a, b = exit_(1, failed=True), exit_(2)
    clients = [client(number, a) for number in range(1, 41)]
    plan = plan_failover([a, b], clients, {}, NOW, RETURN_AFTER)
    assert len(plan.moves) == 40 and {move.reason for move in plan.moves} == {"failover"}


def test_clients_are_spread_by_weight_and_a_tie_goes_to_the_smaller_slot() -> None:
    a, b, c = exit_(1, failed=True), exit_(2, weight=1), exit_(3, weight=2)
    plan = plan_failover([a, b, c], [client(number, a) for number in range(1, 7)], {}, NOW, RETURN_AFTER)
    slots = [slot for _, slot, _ in targets(plan)]
    assert slots == [3, 2, 3, 3, 2, 3]  # вес 2 : 1, ничья (2/2 против 1/1) - в пользу меньшего слота


def test_a_tie_between_equal_exits_goes_to_the_smaller_slot() -> None:
    a, b, c = exit_(1, failed=True), exit_(2), exit_(3)
    plan = plan_failover([c, a, b], [client(1, a)], {}, NOW, RETURN_AFTER)
    assert targets(plan) == [(1, 2, "failover")]


def test_existing_load_counts_when_choosing_the_target() -> None:
    a, b, c = exit_(1, failed=True), exit_(2), exit_(3)
    assignments = [client(1, a), client(2, b), client(3, b)]
    plan = plan_failover([a, b, c], assignments, {}, NOW, RETURN_AFTER)
    assert targets(plan) == [(1, 3, "failover")]  # на втором уже двое


def test_max_clients_is_respected_and_the_rest_are_stranded() -> None:
    a, b = exit_(1, failed=True), exit_(2, max_clients=2)
    plan = plan_failover([a, b], [client(number, a) for number in range(1, 5)], {}, NOW, RETURN_AFTER)
    assert len(plan.moves) == 2 and plan.stranded == {a.exit_id: 2}


def test_no_healthy_exit_changes_nothing_and_reports_the_stranded_clients() -> None:
    a, b = exit_(1, failed=True), exit_(2, failed=True)
    plan = plan_failover([a, b], [client(1, a), client(2, b), client(3, b)], {}, NOW, RETURN_AFTER)
    assert plan.moves == [] and plan.stranded == {a.exit_id: 1, b.exit_id: 2} and not plan.empty


def test_a_suspect_exit_with_one_bad_check_is_neither_failed_nor_a_target() -> None:
    a, b, suspect = exit_(1, failed=True), exit_(2), exit_(3, usable=False)
    plan = plan_failover([a, b, suspect], [client(1, a), client(2, suspect)], {}, NOW, RETURN_AFTER)
    assert targets(plan) == [(1, 2, "failover")]  # клиент подозрительного выхода остаётся, туда никого не отправляют


def test_the_plan_does_not_depend_on_the_input_order() -> None:
    a, b, c = exit_(1, failed=True), exit_(2), exit_(3)
    clients = [client(number, a) for number in range(1, 9)]
    first = plan_failover([a, b, c], clients, {}, NOW, RETURN_AFTER)
    second = plan_failover([c, b, a], list(reversed(clients)), {}, NOW, RETURN_AFTER)
    assert first == second


def test_unpinned_clients_stay_where_they_moved_after_the_exit_returns() -> None:
    a, b = exit_(1, healthy_since=LONG_AGO), exit_(2)  # a давно исправен
    assert plan_failover([a, b], [client(1, b)], {}, NOW, RETURN_AFTER).empty


# --- план: закреплённые ----------------------------------------------------------------------------------------------


def test_a_pinned_client_leaves_a_failed_exit_like_everyone_else() -> None:
    a, b = exit_(1, failed=True), exit_(2)
    c1 = client(1, a)
    plan = plan_failover([a, b], [c1], {c1.credential_id: a.exit_id}, NOW, RETURN_AFTER)
    assert targets(plan) == [(1, 2, "failover")]


def test_a_pinned_client_returns_after_the_pinned_exit_has_been_healthy_long_enough() -> None:
    a, b = exit_(1, healthy_since=NOW - RETURN_AFTER), exit_(2)  # ровно return_after
    c1 = client(1, b)
    plan = plan_failover([a, b], [c1], {c1.credential_id: a.exit_id}, NOW, RETURN_AFTER)
    assert targets(plan) == [(1, 1, "failover_return")]


def test_a_pinned_client_does_not_return_earlier() -> None:
    a, b = exit_(1, healthy_since=NOW - RETURN_AFTER + timedelta(seconds=1)), exit_(2)
    c1 = client(1, b)
    assert plan_failover([a, b], [c1], {c1.credential_id: a.exit_id}, NOW, RETURN_AFTER).empty


def test_a_pinned_client_does_not_return_to_an_exit_that_is_not_healthy_now() -> None:
    c1 = client(1, exit_(2))
    for a in (exit_(1, failed=True), exit_(1, usable=False), exit_(1, healthy_since=None)):
        assert plan_failover([a, exit_(2)], [c1], {c1.credential_id: a.exit_id}, NOW, RETURN_AFTER).moves == []


def test_a_pinned_client_does_not_return_to_a_full_exit() -> None:
    a, b = exit_(1, max_clients=1), exit_(2)
    pinned, other = client(1, b), client(2, a)
    assert plan_failover([a, b], [pinned, other], {pinned.credential_id: a.exit_id}, NOW, RETURN_AFTER).empty


def test_returns_are_limited_per_pass_and_finish_over_several_passes() -> None:
    a, b = exit_(1), exit_(2)
    clients = [client(number, b) for number in range(1, 13)]
    pins = {item.credential_id: a.exit_id for item in clients}
    passes = 0
    state = clients
    while True:
        plan = plan_failover([a, b], state, pins, NOW, RETURN_AFTER)
        if plan.empty:
            break
        assert len(plan.moves) <= MAX_RETURNS_PER_PASS and {m.reason for m in plan.moves} == {"failover_return"}
        state = apply(plan, state)
        passes += 1
    assert passes == 3 and all(item.exit_id == a.exit_id for item in state)  # 5 + 5 + 2


def test_a_pinned_client_on_a_failed_temporary_exit_goes_straight_home_when_home_is_ready() -> None:
    a, b = exit_(1, healthy_since=LONG_AGO), exit_(2, failed=True)
    c1 = client(1, b)
    plan = plan_failover([a, b], [c1], {c1.credential_id: a.exit_id}, NOW, RETURN_AFTER)
    assert targets(plan) == [(1, 1, "failover_return")]


def test_a_pinned_client_whose_only_choice_is_the_failed_pinned_exit_is_stranded() -> None:
    a = exit_(1, failed=True)
    c1 = client(1, a)
    plan = plan_failover([a], [c1], {c1.credential_id: a.exit_id}, NOW, RETURN_AFTER)
    assert plan.moves == [] and plan.stranded == {a.exit_id: 1}


# --- идемпотентность --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("failed", [(1,), (1, 2), (2,)])
def test_applying_a_plan_makes_the_next_plan_empty(failed: tuple[int, ...]) -> None:
    exits = [exit_(slot, failed=slot in failed, weight=slot) for slot in (1, 2, 3)]
    by_slot = {view.slot: view for view in exits}
    clients = [client(number, by_slot[1 + number % 3]) for number in range(1, 31)]
    pins = {clients[0].credential_id: by_slot[1].exit_id, clients[1].credential_id: by_slot[2].exit_id}
    plan = plan_failover(exits, clients, pins, NOW, RETURN_AFTER)
    assert plan_failover(exits, apply(plan, clients), pins, NOW, RETURN_AFTER).moves == []


def test_two_identical_calls_give_the_same_plan() -> None:
    a, b = exit_(1, failed=True), exit_(2)
    clients = [client(number, a) for number in range(1, 6)]
    assert plan_failover([a, b], clients, {}, NOW, RETURN_AFTER) == plan_failover(
        [a, b], clients, {}, NOW, RETURN_AFTER
    )


def test_unknown_exits_in_assignments_are_ignored() -> None:
    a = exit_(1)
    ghost = AssignmentView(uuid4(), uuid4())
    assert plan_failover([a], [ghost], {}, NOW, RETURN_AFTER).empty
