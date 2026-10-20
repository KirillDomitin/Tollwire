"""Failover выходов шлюза и ручной перенос / закрепление (этап 24.5) через планировщик, воркер и HTTP. Шлюз и выходы -
несколько FakeServer (см. test_gateway_exits_api.py); «рукопожатие устарело» - `FakeServer.exit_handshakes[slot]`."""

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, update

from core.config import settings
from core.database.postgres import session_factory
from core.ssh_exception import CommandExecutionError
from infrastructure.workers.scheduler import Scheduler, build_rules, watch_gateways
from infrastructure.workers.worker import JobWorker
from modules.access.models.vpn_credential import VPNCredential
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.gateway import failover as failover_module
from modules.gateway.models.gateway_exit import ExitState, GatewayExit
from modules.jobs.models.job import JobStatus, JobType
from modules.servers.models.server import HealthStatus, VPNServer
from tests.fixtures import World
from tests.integration.conftest import drain
from tests.integration.test_gateway_assignments_api import (
    address_of,
    assignments,
    chain,
    client,
    gateway_with_exits,
)
from tests.integration.test_gateway_exits_api import Scene, exits_url, list_exits
from tests.integration.test_mvp_flow import run_job

STALE = 3600  # возраст рукопожатия, при котором выход «не работает»


def fresh() -> int:
    return int(time.time()) - 5


def stale() -> int:
    return int(time.time()) - STALE


async def tick(worker: JobWorker) -> str:
    """Один проход правила gateway_watch и выполнение поставленных им задач."""
    summary = await watch_gateways()
    await drain(worker)
    return summary


async def db_exit(slot: int) -> GatewayExit:
    async with session_factory() as session:
        return (await session.execute(select(GatewayExit).where(GatewayExit.slot == slot))).scalar_one()


async def set_exit(slot: int, **values: Any) -> None:
    async with session_factory() as session:
        await session.execute(update(GatewayExit).where(GatewayExit.slot == slot).values(**values))
        await session.commit()


async def audit(*actions: AuditAction) -> list[AuditLog]:
    async with session_factory() as session:
        query = select(AuditLog).where(AuditLog.action.in_(actions)).order_by(AuditLog.created_at)
        return list((await session.execute(query)).scalars())


async def apply_jobs(world: World) -> list[Any]:
    world.session.expire_all()
    return await world.jobs(JobType.GATEWAY_APPLY)


def by_credential(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["credential_id"]: item for item in items}


async def scene_with_clients(
    api: httpx.AsyncClient, worker: JobWorker, world: World, names: tuple[str, ...] = ("a1", "a2")
) -> tuple[Scene, dict[str, Any], list[dict[str, Any]], list[str]]:
    gw, protocol, exits = await gateway_with_exits(api, worker, world)
    ids = [await client(api, worker, gw, protocol, name) for name in names]
    return gw, protocol, exits, ids


def patch_url(sid: UUID, credential_id: str) -> str:
    return f"/api/v1/servers/{sid}/gateway/assignments/{credential_id}"


# --- гистерезис и failover -------------------------------------------------------------------------------------


async def test_one_bad_check_is_only_a_counter(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)
    before = {item["credential_id"]: item["slot"] for item in await assignments(api, gw.sid)}
    gw.gateway.exit_handshakes[1] = stale()

    await tick(worker)

    row = await db_exit(1)
    assert (row.consecutive_failures, row.health, row.healthy_since) == (1, "stale", None)
    assert await apply_jobs(world) == []
    assert {item["credential_id"]: item["slot"] for item in await assignments(api, gw.sid)} == before


async def test_two_bad_checks_move_the_clients_to_the_healthy_exit(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, ids = await scene_with_clients(api, worker, world, ("a1", "a2", "a3"))
    on_first = [item for item in await assignments(api, gw.sid) if item["slot"] == 1]
    assert on_first
    gw.gateway.exit_handshakes[1] = stale()

    await tick(worker)
    await tick(worker)

    assert {item["slot"] for item in await assignments(api, gw.sid)} == {2}
    assert set(chain(gw).values()) == {1002}  # карта на шлюзе применена
    moved = [item for item in await assignments(api, gw.sid) if item["reason"] == "failover"]
    assert {item["credential_id"] for item in moved} == {item["credential_id"] for item in on_first}
    (job,) = await apply_jobs(world)
    assert job.status == JobStatus.SUCCESS and job.result["moved"] == len(on_first) and job.server_id == gw.sid
    events = await audit(AuditAction.GATEWAY_CLIENT_MOVED)
    assert len(events) == len(on_first) and {event.meta["reason"] for event in events} == {"failover"}
    assert all(event.meta["from_slot"] == 1 and event.meta["to_slot"] == 2 for event in events)
    (failover,) = await audit(AuditAction.GATEWAY_FAILOVER)
    assert failover.meta["slot"] == 1 and failover.meta["moved"] == len(on_first) and failover.meta["unassigned"] == 0
    assert "10.77." not in str([event.meta for event in events]) + str(failover.meta)  # адресов клиентов нет


async def test_a_good_check_after_one_bad_resets_the_counter_without_any_failover(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)
    gw.gateway.exit_handshakes[1] = stale()
    await tick(worker)
    gw.gateway.exit_handshakes[1] = fresh()

    await tick(worker)

    row = await db_exit(1)
    assert row.consecutive_failures == 0 and row.healthy_since is not None
    assert await apply_jobs(world) == [] and await audit(AuditAction.GATEWAY_EXIT_RECOVERED) == []


async def test_a_recovered_exit_is_audited_and_unpinned_clients_stay_where_they_moved(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)
    gw.gateway.exit_handshakes[1] = stale()
    await tick(worker)
    await tick(worker)
    gw.gateway.exit_handshakes[1] = fresh()

    await tick(worker)

    row = await db_exit(1)
    assert row.consecutive_failures == 0 and row.healthy_since is not None
    (recovered,) = await audit(AuditAction.GATEWAY_EXIT_RECOVERED)
    assert recovered.meta["slot"] == 1
    assert {item["slot"] for item in await assignments(api, gw.sid)} == {2}  # автоматического возврата нет
    exit_one = next(item for item in await list_exits(api, gw.sid) if item["slot"] == 1)
    assert exit_one["healthy_since"] is not None and "consecutive_failures" not in exit_one


async def test_a_pinned_client_returns_home_only_after_the_exit_was_healthy_long_enough(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, exits, ids = await scene_with_clients(api, worker, world, ("a1",))
    (pinned,) = ids
    assert (
        await api.patch(patch_url(gw.sid, pinned), json={"exit_id": exits[0]["id"], "pin": True})
    ).status_code == 200
    gw.gateway.exit_handshakes[1] = stale()
    await tick(worker)
    await tick(worker)
    assert by_credential(await assignments(api, gw.sid))[pinned]["slot"] == 2  # уехал

    gw.gateway.exit_handshakes[1] = fresh()
    await tick(worker)  # восстановился, но десяти минут ещё нет
    item = by_credential(await assignments(api, gw.sid))[pinned]
    assert item["slot"] == 2 and item["pinned_exit_id"] == exits[0]["id"]

    await set_exit(1, healthy_since=datetime.now(timezone.utc) - timedelta(seconds=601))
    await tick(worker)  # return_after прошёл

    item = by_credential(await assignments(api, gw.sid))[pinned]
    assert item["slot"] == 1 and item["reason"] == "failover_return" and item["pinned_exit_id"] == exits[0]["id"]
    assert set(chain(gw).values()) == {1001}
    assert [event.meta["reason"] for event in await audit(AuditAction.GATEWAY_CLIENT_MOVED)][-1] == "failover_return"


async def test_pinned_and_unpinned_clients_behave_differently_after_the_exit_returns(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, exits, (pinned, loose) = await scene_with_clients(api, worker, world, ("p1", "p2"))
    items = by_credential(await assignments(api, gw.sid))
    # оба на первом выходе: закрепляем одного, второго переносим вручную туда же
    assert (
        await api.patch(patch_url(gw.sid, pinned), json={"exit_id": exits[0]["id"], "pin": True})
    ).status_code == 200
    assert (
        await api.patch(patch_url(gw.sid, loose), json={"exit_id": exits[0]["id"], "pin": False})
    ).status_code == 200
    gw.gateway.exit_handshakes[1] = stale()
    await tick(worker)
    await tick(worker)
    gw.gateway.exit_handshakes[1] = fresh()
    await tick(worker)
    await set_exit(1, healthy_since=datetime.now(timezone.utc) - timedelta(hours=1))
    await tick(worker)

    items = by_credential(await assignments(api, gw.sid))
    assert items[pinned]["slot"] == 1 and items[loose]["slot"] == 2


async def test_when_every_exit_is_bad_nothing_is_counted_or_moved_and_the_outage_is_audited_once(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world, ("a1", "a2", "a3"))
    before = by_credential(await assignments(api, gw.sid))
    map_before, applies = chain(gw), gw.gateway.assign_applies
    gw.gateway.exit_handshakes[1] = stale()
    gw.gateway.exit_handshakes[2] = stale()

    for _ in range(6):
        await tick(worker)

    assert by_credential(await assignments(api, gw.sid)) == before  # клиенты остались на «мёртвых»: kill-switch
    assert chain(gw) == map_before and gw.gateway.assign_applies == applies  # карту не трогали
    assert [(await db_exit(slot)).consecutive_failures for slot in (1, 2)] == [0, 0]  # счётчики не растут
    assert [(await db_exit(slot)).health for slot in (1, 2)] == ["stale", "stale"]
    (event,) = await audit(AuditAction.GATEWAY_ALL_EXITS_DOWN)  # один раз, не на каждый тик
    assert event.meta == {"server_id": str(gw.sid), "exits": 2}
    assert await apply_jobs(world) == [] and await audit(AuditAction.GATEWAY_FAILOVER) == []
    assert await audit(AuditAction.GATEWAY_CLIENT_MOVED) == []


async def test_a_gateway_restart_with_no_tunnels_moves_nobody_and_recovers_without_a_stampede(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world, ("a1", "a2", "a3", "a4"))
    before = by_credential(await assignments(api, gw.sid))
    interfaces = list(gw.gateway.exit_interfaces)
    gw.gateway.exit_interfaces.clear()  # контейнер шлюза перезапущен: `awg show interfaces` пусто

    for _ in range(4):
        await tick(worker)

    assert by_credential(await assignments(api, gw.sid)) == before and await apply_jobs(world) == []
    assert [(await db_exit(slot)).consecutive_failures for slot in (1, 2)] == [0, 0]
    assert len(await audit(AuditAction.GATEWAY_ALL_EXITS_DOWN)) == 1

    gw.gateway.exit_interfaces.extend(interfaces)  # туннели поднялись
    await tick(worker)
    await tick(worker)

    assert by_credential(await assignments(api, gw.sid)) == before  # ни одного переноса
    rows = [await db_exit(slot) for slot in (1, 2)]
    assert [row.consecutive_failures for row in rows] == [0, 0] and all(row.healthy_since for row in rows)
    assert sorted(event.meta["slot"] for event in await audit(AuditAction.GATEWAY_EXIT_RECOVERED)) == [1, 2]
    assert len(await audit(AuditAction.GATEWAY_ALL_EXITS_DOWN)) == 1  # повторного события нет
    assert await audit(AuditAction.GATEWAY_CLIENT_MOVED) == []


async def test_a_single_exit_gateway_never_fails_over_but_reports_the_outage(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world, count=1)
    await client(api, worker, gw, protocol, "solo")
    gw.gateway.exit_handshakes[1] = stale()

    for _ in range(3):
        await tick(worker)

    assert (await db_exit(1)).consecutive_failures == 0 and len(await audit(AuditAction.GATEWAY_ALL_EXITS_DOWN)) == 1


async def test_the_stranded_failover_event_is_not_lost_when_the_job_runs_late(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, exits, _ = await scene_with_clients(api, worker, world, ("a1", "a2", "a3"))
    assert (await api.patch(exits_url(gw.sid, exits[1]["id"]), json={"max_clients": 1})).status_code == 200
    on_first = [item for item in await assignments(api, gw.sid) if item["slot"] == 1]
    assert len(on_first) == 2  # второй выход полон (один клиент, предел 1) - первым некуда деться
    gw.gateway.exit_handshakes[1] = stale()

    for _ in range(4):  # задача поставлена на втором тике, а выполняется, когда счётчик уже 4
        await watch_gateways()
    assert (await db_exit(1)).consecutive_failures == 4
    await drain(worker)
    await tick(worker)
    await tick(worker)

    (event,) = await audit(AuditAction.GATEWAY_FAILOVER)  # ровно одно событие за отказ, не потерянное и не повторённое
    assert event.meta["slot"] == 1 and event.meta["moved"] == 0 and event.meta["unassigned"] == 2
    assert (await db_exit(1)).failure_notified is True
    gw.gateway.exit_handshakes[1] = fresh()
    await tick(worker)
    assert (await db_exit(1)).failure_notified is False  # сброшено при восстановлении


async def test_a_repeated_pass_on_an_applied_state_does_nothing(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world, ("a1", "a2"))
    gw.gateway.exit_handshakes[1] = stale()
    await tick(worker)
    await tick(worker)
    jobs, applies, commands = len(await apply_jobs(world)), gw.gateway.assign_applies, len(gw.gateway.commands)

    for _ in range(3):
        await tick(worker)

    assert len(await apply_jobs(world)) == jobs and gw.gateway.assign_applies == applies
    assert not [c for c in gw.gateway.commands[commands:] if "iptables-restore" in c]  # лишних apply нет


async def test_the_watch_rule_does_not_queue_duplicates_while_a_job_is_unfinished(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)
    gw.gateway.exit_handshakes[1] = stale()
    await watch_gateways()
    await watch_gateways()  # на втором проходе выход стал отказавшим - задача поставлена
    await watch_gateways()
    await watch_gateways()

    pending = [job for job in await apply_jobs(world) if job.status == JobStatus.PENDING]
    assert len(pending) == 1
    await drain(worker)


async def test_ssh_errors_do_not_break_the_rule_and_leave_the_counters(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)

    async def broken(self: Any, gateway: Any, provider: Any) -> Any:
        raise CommandExecutionError("ssh is down")

    monkeypatch.setattr(failover_module.GatewayFailoverService, "check", broken)

    summary = await watch_gateways()

    assert "1 unreachable" in summary and (await db_exit(1)).consecutive_failures == 0


async def test_gateways_without_exits_are_not_polled_and_no_gateways_is_fine(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    assert (await watch_gateways()).startswith("checked 0 gateways, queued 0")
    sid, fake = await _bare_gateway(api, worker, world)
    commands = len(fake.commands)

    summary = await watch_gateways()

    assert summary.startswith("checked 0 gateways") and len(fake.commands) == commands  # SSH не открывался


async def _bare_gateway(api: httpx.AsyncClient, worker: JobWorker, world: World) -> tuple[UUID, Any]:
    from tests.integration.test_gateway_api import gateway_server, install_gateway

    sid, fake = await gateway_server(world)
    await install_gateway(api, worker, sid)
    return sid, fake


async def test_a_down_exit_server_with_a_live_tunnel_is_not_a_failure(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, exits, _ = await scene_with_clients(api, worker, world)
    before = by_credential(await assignments(api, gw.sid))
    async with session_factory() as session:
        server = await session.get(VPNServer, UUID(exits[0]["exit_server_id"]))
        assert server is not None
        server.health_status = HealthStatus.DOWN  # статус сервера обновляется редко и не зависит от туннеля
        await session.commit()

    for _ in range(3):
        await tick(worker)

    assert (await db_exit(1)).consecutive_failures == 0 and await apply_jobs(world) == []
    assert by_credential(await assignments(api, gw.sid)) == before


async def test_exits_that_are_not_active_do_not_take_part(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)
    await set_exit(1, state=ExitState.DRAINING)
    gw.gateway.exit_handshakes[1] = stale()

    await tick(worker)
    await tick(worker)

    assert (await db_exit(1)).consecutive_failures == 0 and await apply_jobs(world) == []


async def test_a_suspect_exit_does_not_get_new_clients(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, protocol, _, _ = await scene_with_clients(api, worker, world, ("a1",))
    gw.gateway.exit_handshakes[2] = stale()
    await tick(worker)  # у второго одна плохая проверка

    newcomer = await client(api, worker, gw, protocol, "late")

    assert by_credential(await assignments(api, gw.sid))[newcomer]["slot"] == 1


async def test_the_rule_is_registered_and_runs_every_minute() -> None:
    (rule,) = [item for item in build_rules(settings) if item.name == "gateway_watch"]
    assert rule.interval == settings.gateway_watch_interval_seconds == 60 and rule.action is watch_gateways
    assert [item.name for item in Scheduler([rule], tick_seconds=1).rules] == ["gateway_watch"]


# --- ручной перенос и закрепление ------------------------------------------------------------------------------


async def test_a_manual_move_changes_the_exit_and_the_map(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, exits, (first, _) = await scene_with_clients(api, worker, world)
    current = by_credential(await assignments(api, gw.sid))[first]
    target = next(item for item in exits if item["id"] != current["exit_id"])

    response = await api.patch(patch_url(gw.sid, first), json={"exit_id": target["id"]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["exit_id"] == target["id"] and body["slot"] == target["slot"] and body["reason"] == "manual"
    assert body["pinned_exit_id"] is None
    assert chain(gw)[await address_of(first)] == 1000 + target["slot"]
    (event,) = await audit(AuditAction.GATEWAY_CLIENT_MOVED)
    assert event.meta["reason"] == "manual" and event.meta["to_slot"] == target["slot"]
    assert event.meta["from_slot"] == current["slot"] and "10.77." not in str(event.meta)


async def test_pin_unpin_and_pin_the_current_exit(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, _, exits, (first, _) = await scene_with_clients(api, worker, world)
    slot_before = by_credential(await assignments(api, gw.sid))[first]["slot"]

    pinned = await api.patch(patch_url(gw.sid, first), json={"exit_id": None, "pin": True})  # за текущим
    assert pinned.status_code == 200 and pinned.json()["slot"] == slot_before
    pinned_exit = pinned.json()["pinned_exit_id"]
    assert pinned_exit == pinned.json()["exit_id"]
    assert by_credential(await assignments(api, gw.sid))[first]["pinned_exit_id"] == pinned_exit

    applies = gw.gateway.assign_applies
    unpinned = await api.patch(patch_url(gw.sid, first), json={"exit_id": None, "pin": False})  # только снять
    assert unpinned.status_code == 200 and unpinned.json()["pinned_exit_id"] is None
    assert unpinned.json()["slot"] == slot_before and gw.gateway.assign_applies == applies  # карту не трогали
    assert [event.meta["reason"] for event in await audit(AuditAction.GATEWAY_CLIENT_MOVED)] == ["pin", "unpin"]


async def test_move_with_pin_pins_to_the_target_and_move_without_pin_releases_it(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, exits, (first, _) = await scene_with_clients(api, worker, world)
    one, two = exits

    moved = await api.patch(patch_url(gw.sid, first), json={"exit_id": two["id"], "pin": True})
    assert moved.json()["pinned_exit_id"] == two["id"] and moved.json()["slot"] == 2

    back = await api.patch(patch_url(gw.sid, first), json={"exit_id": one["id"]})
    assert back.json()["slot"] == 1 and back.json()["pinned_exit_id"] is None


async def test_manual_move_errors(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, protocol, exits, (first, _) = await scene_with_clients(api, worker, world)
    other = await scene_other_gateway(api, worker, world)

    assert (await api.patch(patch_url(uuid4(), first), json={})).status_code == 404  # нет сервера
    assert (await api.patch(patch_url(gw.sid, str(uuid4())), json={"exit_id": exits[1]["id"]})).status_code == 404
    assert (await api.patch(patch_url(gw.sid, first), json={"exit_id": str(uuid4())})).status_code == 404
    assert (
        await api.patch(patch_url(other.sid, first), json={"exit_id": exits[1]["id"]})
    ).status_code == 404  # чужой шлюз
    assert (await api.patch(patch_url(gw.sid, first), json={"exit_id": other_exit_id(other)})).status_code == 404
    assert (await api.patch(patch_url(gw.sid, first), json={"exit_id": "not-a-uuid"})).status_code == 422
    assert (await api.patch(patch_url(gw.sid, first), json={"pin": "maybe"})).status_code == 422

    await set_exit(2, state=ExitState.DRAINING)
    assert (await api.patch(patch_url(gw.sid, first), json={"exit_id": exits[1]["id"]})).status_code == 409  # не ACTIVE
    await set_exit(2, state=ExitState.ACTIVE, consecutive_failures=2)
    assert (await api.patch(patch_url(gw.sid, first), json={"exit_id": exits[1]["id"]})).status_code == 409  # отказал


def other_exit_id(scene: Scene) -> str:
    return getattr(scene, "exit_id")


async def scene_other_gateway(api: httpx.AsyncClient, worker: JobWorker, world: World) -> Scene:
    """Второй шлюз со своим выходом: его credential и выходы чужие для первого."""
    gw2, protocol2, exits2 = await gateway_with_exits(api, worker, world, count=1)
    gw2.exit_id = exits2[0]["id"]  # type: ignore[attr-defined]
    gw2.client = await client(api, worker, gw2, protocol2, "other-client")  # type: ignore[attr-defined]
    return gw2


async def test_a_revoked_client_and_a_client_without_an_exit_are_409(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, exits, (first, _) = await scene_with_clients(api, worker, world)
    async with session_factory() as session:
        row = await session.get(VPNCredential, UUID(first))
        assert row is not None
        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    assert (await api.patch(patch_url(gw.sid, first), json={"exit_id": exits[0]["id"]})).status_code == 409

    gw_bare = await scene_other_gateway(api, worker, world)
    # у клиента ещё нет назначения: снять закрепление нечего
    async with session_factory() as session:
        from modules.gateway.models.gateway_assignment import GatewayAssignment

        await session.execute(GatewayAssignment.__table__.delete())  # type: ignore[attr-defined]
        await session.commit()
    assert (await api.patch(patch_url(gw_bare.sid, gw_bare.client), json={})).status_code == 409  # type: ignore[attr-defined]
    assert (await api.patch(patch_url(gw_bare.sid, gw_bare.client), json={"pin": True})).status_code == 409  # type: ignore[attr-defined]
    ok = await api.patch(patch_url(gw_bare.sid, gw_bare.client), json={"exit_id": gw_bare.exit_id})  # type: ignore[attr-defined]
    assert ok.status_code == 200 and ok.json()["reason"] == "manual"  # без назначения вручную назначить можно


async def test_a_busy_gateway_is_409(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    from infrastructure.workers.locks import ServerLock

    gw, _, exits, (first, _) = await scene_with_clients(api, worker, world)
    async with ServerLock(gw.sid, blocking_timeout=0):
        response = await api.patch(patch_url(gw.sid, first), json={"exit_id": exits[1]["id"]})
    assert response.status_code == 409 and "busy" in response.json()["detail"]


# --- «проверить сейчас» ------------------------------------------------------------------------------------------


async def test_check_now_queues_a_job_and_does_not_bypass_the_hysteresis(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)
    gw.gateway.exit_handshakes[1] = stale()
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    await set_exit(1, checked_at=old)

    first = await api.post(f"/api/v1/servers/{gw.sid}/gateway/check")
    assert first.status_code == 202 and first.json()["type"] == "gateway_apply" and "params" not in first.json()
    assert (await api.post(f"/api/v1/servers/{gw.sid}/gateway/check")).status_code == 409  # такая задача уже идёт
    job = await run_job(api, worker, first)
    assert (
        job["status"] == "success" and (await db_exit(1)).consecutive_failures == 1
    )  # интервал прошёл: проба засчитана

    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/gateway/check"))
    row = await db_exit(1)
    assert job["status"] == "success" and job["result"]["moved"] == 0
    assert row.consecutive_failures == 1 and row.health == "stale"  # вторая проба через секунды не засчитана
    assert {item["slot"] for item in await assignments(api, gw.sid)} == {1, 2}

    await set_exit(1, checked_at=old)  # прошла половина интервала gateway_watch
    job = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/gateway/check"))
    assert job["status"] == "success" and job["result"]["moved"] >= 1 and (await db_exit(1)).consecutive_failures == 2
    assert {item["slot"] for item in await assignments(api, gw.sid)} == {2}


async def test_the_scheduler_counter_is_not_blocked_by_a_manual_check_next_to_it(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)
    gw.gateway.exit_handshakes[1] = stale()
    await tick(worker)  # счётчик 1
    await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/gateway/check"))
    assert (await db_exit(1)).consecutive_failures == 1  # ручная проверка рядом не добавила

    await tick(worker)  # планировщик - вторая проба

    assert (await db_exit(1)).consecutive_failures == 2 and {
        item["slot"] for item in await assignments(api, gw.sid)
    } == {2}


async def test_check_now_errors(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    assert (await api.post(f"/api/v1/servers/{uuid4()}/gateway/check")).status_code == 404
    plain, _, _ = await world.server()
    assert (await api.post(f"/api/v1/servers/{plain.id}/gateway/check")).status_code == 409  # шлюза нет


async def test_the_exit_list_does_not_leak_the_counter(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, _, _, _ = await scene_with_clients(api, worker, world)
    await tick(worker)

    (first, _) = await list_exits(api, gw.sid)

    assert {"health", "last_handshake_at", "healthy_since"} <= set(first) and "consecutive_failures" not in first


# --- ревью 24.5: параллельность и срок правила, порядок в PATCH, 502 --------------------------------------------------


async def add_bare_gateways(world: World, count: int) -> list[UUID]:
    """Серверы с протоколом шлюза в БД (без контейнеров): для проверки самого планировщика."""
    from modules.protocols.models.protocol import ProtocolType, VPNProtocol

    ids = []
    for _ in range(count):
        server, _, _ = await world.server(install=False)
        world.session.add(VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_GATEWAY, port=1))
        ids.append(server.id)
    await world.session.commit()
    return ids


async def test_gateways_are_polled_in_parallel_with_a_limit(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from infrastructure.workers import scheduler as scheduler_module

    await add_bare_gateways(world, 8)
    running = peak = 0

    async def slow(self: Any, gateway: Any) -> Any:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.2)
        running -= 1
        return failover_module.WatchResult(checked=True)

    monkeypatch.setattr(failover_module.GatewayFailoverService, "watch", slow)
    summary = await watch_gateways()

    assert summary == "checked 8 gateways, queued 0 applies, 0 unreachable"
    assert 2 <= peak <= scheduler_module.WATCH_CONCURRENCY  # параллельно, но не больше предела


async def test_a_slow_or_broken_gateway_does_not_hold_the_others_or_change_its_counters(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from infrastructure.workers import scheduler as scheduler_module

    gw, _, _, _ = await scene_with_clients(api, worker, world)
    await add_bare_gateways(world, 2)
    gw.gateway.exit_handshakes[1] = stale()
    real_check = failover_module.GatewayFailoverService.check

    async def hanging_check(self: Any, gateway: Any, provider: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(5)
        return await real_check(self, gateway, provider, **kwargs)

    async def quick(self: Any, gateway: Any) -> Any:
        return failover_module.WatchResult(checked=True)

    real_watch = failover_module.GatewayFailoverService.watch

    async def route(self: Any, gateway: Any) -> Any:
        return await (real_watch if gateway.server_id == gw.sid else quick)(self, gateway)

    monkeypatch.setattr(scheduler_module, "WATCH_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(failover_module.GatewayFailoverService, "check", hanging_check)
    monkeypatch.setattr(failover_module.GatewayFailoverService, "watch", route)
    started = time.monotonic()

    summary = await watch_gateways()

    assert summary == "checked 2 gateways, queued 0 applies, 1 unreachable"  # зависший шлюз отсечён по сроку
    assert time.monotonic() - started < 3
    row = await db_exit(1)
    assert row.consecutive_failures == 0 and row.health != "stale"  # его счётчики не тронуты


async def test_a_manual_move_makes_the_gateway_ready_before_it_touches_the_assignments(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modules.gateway.assignment import GatewayAssignmentService
    from modules.gateway.models.gateway_assignment import GatewayAssignment
    from tests.integration.test_gateway_assignments_api import make_outdated

    gw, _, exits, (first, _) = await scene_with_clients(api, worker, world)
    current = by_credential(await assignments(api, gw.sid))[first]
    target = next(item for item in exits if item["id"] != current["exit_id"])
    make_outdated(gw)  # sysctl прежней версии: install() пересоздаст контейнер (и закоммитит)
    seen: list[UUID] = []
    original = GatewayAssignmentService._ready

    async def spy(self: Any, gateway: Any, provider: Any) -> None:
        row = await self.session.get(GatewayAssignment, UUID(first))
        assert row is not None
        seen.append(row.exit_id)  # назначение на момент готовности шлюза
        await original(self, gateway, provider)

    monkeypatch.setattr(GatewayAssignmentService, "_ready", spy)

    response = await api.patch(patch_url(gw.sid, first), json={"exit_id": target["id"]})

    assert response.status_code == 200, response.text
    assert seen == [UUID(current["exit_id"])]  # к моменту _ready (он коммитит при пересоздании) БД ещё не менялась
    assert response.json()["exit_id"] == target["id"]
    (entry,) = await world.audit(AuditAction.GATEWAY_CONTAINER_RECREATED, gw.sid)
    assert entry.meta["reason"] == "sysctl_mismatch"
    assert chain(gw)[await address_of(first)] == 1000 + target["slot"]


async def test_a_gateway_failure_while_moving_is_a_502_without_details_and_nothing_is_saved(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modules.gateway.assignment import GatewayAssignmentService

    gw, _, exits, (first, _) = await scene_with_clients(api, worker, world)
    before = by_credential(await assignments(api, gw.sid))[first]
    target = next(item for item in exits if item["id"] != before["exit_id"])

    async def broken(self: Any, gateway: Any, provider: Any) -> None:
        raise CommandExecutionError("iptables-restore failed: /opt/vpn-admin/gw/assign.rules secret-detail")

    monkeypatch.setattr(GatewayAssignmentService, "_apply_map", broken)

    response = await api.patch(patch_url(gw.sid, first), json={"exit_id": target["id"], "pin": True})

    assert response.status_code == 502, response.text
    assert "secret-detail" not in response.text and "assign.rules" not in response.text
    assert by_credential(await assignments(api, gw.sid))[first] == before  # назначение и закрепление не сохранены
    assert await audit(AuditAction.GATEWAY_CLIENT_MOVED) == []
