"""Назначение выходов клиентам шлюза (этап 24.3) через HTTP и воркер: выдача и отзыв, перенос при снятии выхода, sync,
права. Шлюз и выходы - несколько FakeServer одновременно (см. test_gateway_exits_api.py)."""

import json
import time
from ipaddress import IPv4Address
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from core.database.postgres import session_factory
from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT, exit_host_file, render_assign_rules
from infrastructure.workers.worker import JobWorker
from modules.access.models.vpn_credential import VPNCredential
from modules.audit.models.audit_log import AuditAction
from modules.gateway.models.gateway_assignment import GatewayAssignment
from modules.gateway.models.gateway_exit import ExitState, GatewayExit
from modules.jobs.models.job import JobType
from tests.fixtures import World
from tests.integration.conftest import drain
from tests.integration.test_gateway_api import issue_gateway_client
from tests.integration.test_gateway_exits_api import (
    Scene,
    add_exit,
    exit_server,
    exits_url,
    list_exits,
    scene,
)
from tests.integration.test_mvp_flow import run_job

ASSIGN_FILE = f"{GATEWAY_LAYOUT.host_config_dir}/assign.rules"


async def gateway_with_exits(
    api: httpx.AsyncClient, worker: JobWorker, world: World, count: int = 2
) -> tuple[Scene, dict[str, Any], list[dict[str, Any]]]:
    """Шлюз и `count` выходов; на шлюзе у выходов свежие рукопожатия (годятся для новых клиентов)."""
    gw = await scene(api, worker, world)
    (protocol,) = (await api.get(f"/api/v1/servers/{gw.sid}/protocols")).json()
    added = []
    for index in range(count):
        server, _, _ = await exit_server(world, f"198.51.100.{20 + index}")
        added.append(await add_exit(api, worker, gw.sid, server.id))
        gw.gateway.exit_handshakes[added[-1]["slot"]] = int(time.time()) - 5
    return gw, protocol, added


async def client(api: httpx.AsyncClient, worker: JobWorker, gw: Scene, protocol: dict[str, Any], name: str) -> str:
    """Выдаёт клиента шлюза; возвращает id его credential."""
    _, credential = await issue_gateway_client(api, worker, gw.sid, protocol["id"], username=name)
    return str(credential["id"])


async def assignments(api: httpx.AsyncClient, sid: UUID) -> list[dict[str, Any]]:
    response = await api.get(f"/api/v1/servers/{sid}/gateway/assignments")
    assert response.status_code == 200, response.text
    return list(response.json())


async def address_of(credential_id: str) -> str:
    async with session_factory() as session:
        row = await session.get(VPNCredential, UUID(credential_id))
        assert row is not None
        return row.client_address


def chain(gw: Scene) -> dict[str, int]:
    """Фактическая цепочка шлюза: адрес -> метка."""
    return dict(gw.gateway.assign_chain)


# --- выдача --------------------------------------------------------------------------------------------------------


async def test_a_new_client_gets_an_exit_and_the_map_is_applied(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, _) = await gateway_with_exits(api, worker, world)

    credential_id = await client(api, worker, gw, protocol, "alice")

    (item,) = await assignments(api, gw.sid)
    assert item["credential_id"] == credential_id and item["slot"] == 1 and item["reason"] == "issue"
    assert item["exit_id"] == first["id"]
    assert chain(gw) == {await address_of(credential_id): 1001}
    assert gw.gateway.files[ASSIGN_FILE].count("-A GW_ASSIGN") == 1 and gw.gateway.file_modes[ASSIGN_FILE] == 0o600


async def test_clients_spread_over_exits_by_load_and_the_exit_list_counts_them(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    for name in ("a1", "a2", "a3"):
        await client(api, worker, gw, protocol, name)

    slots = sorted(item["slot"] for item in await assignments(api, gw.sid))
    assert slots == [1, 1, 2]
    assert [item["assigned_clients"] for item in await list_exits(api, gw.sid)] == [2, 1]
    assert sorted(chain(gw).values()) == [1001, 1001, 1002]


async def test_weights_and_limits_steer_the_choice(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, protocol, (first, _) = await gateway_with_exits(api, worker, world)
    assert (await api.patch(exits_url(gw.sid, first["id"]), json={"max_clients": 1})).status_code == 200
    for name in ("b1", "b2", "b3"):
        await client(api, worker, gw, protocol, name)
    assert sorted(item["slot"] for item in await assignments(api, gw.sid)) == [1, 2, 2]  # предел первого - 1 клиент


async def test_unhealthy_exits_get_no_new_clients(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, protocol, (_, _) = await gateway_with_exits(api, worker, world)
    gw.gateway.exit_handshakes[1] = int(time.time()) - 3600  # устаревшее рукопожатие первого
    await client(api, worker, gw, protocol, "c1")
    gw.gateway.exit_interfaces.remove("gwx2")  # второго нет на шлюзе совсем: ни интерфейса, ни файла
    gw.gateway.exit_configs.pop("gwx2", None)
    gw.gateway.files.pop(exit_host_file(GATEWAY_LAYOUT, 2))
    await client(api, worker, gw, protocol, "c2")

    assert [item["slot"] for item in await assignments(api, gw.sid)] == [2]  # второй клиент выхода не получил
    assert len(chain(gw)) == 1


async def test_no_exits_the_client_keeps_the_config_without_an_exit_and_gets_one_when_an_exit_appears(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw = await scene(api, worker, world)
    (protocol,) = (await api.get(f"/api/v1/servers/{gw.sid}/protocols")).json()

    first = await client(api, worker, gw, protocol, "d1")  # выдача проходит, выходов нет

    assert await assignments(api, gw.sid) == [] and chain(gw) == {}
    assert (await api.post(f"/api/v1/credentials/{first}/config")).status_code == 200  # конфиг выдан и не потерян
    server, _, _ = await exit_server(world, "198.51.100.20")
    job = await run_job(
        api, worker, await api.post(f"/api/v1/servers/{gw.sid}/gateway/exits", json={"exit_server_id": str(server.id)})
    )
    assert job["status"] == "success" and job["result"]["assigned"] == 1
    (item,) = await assignments(api, gw.sid)
    assert item["credential_id"] == first and item["reason"] == "issue"
    assert chain(gw) == {await address_of(first): 1001}


async def test_issue_still_succeeds_when_applying_the_map_fails_and_sync_repairs_it(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    gw.gateway.assign_test_fails = True
    issued = await issue_failing(api, worker, gw, protocol)
    assert issued["job"]["status"] == "failed"
    # Выданный клиент и его назначение в БД остались (БД - источник правды), на шлюзе карта пока старая.
    assert len(await assignments(api, gw.sid)) == 1 and chain(gw) == {}
    gw.gateway.assign_test_fails = False

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert report["assignments"]["reapplied"] is True and len(chain(gw)) == 1


async def issue_failing(
    api: httpx.AsyncClient, worker: JobWorker, gw: Scene, protocol: dict[str, Any]
) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    user = (await api.post("/api/v1/vpn-users", json={"username": "e1", "email": "e1@example.com"})).json()
    period = {
        "valid_from": (now - timedelta(minutes=1)).isoformat(),
        "valid_until": (now + timedelta(days=30)).isoformat(),
    }
    subscription = (await api.post(f"/api/v1/vpn-users/{user['id']}/subscriptions", json=period)).json()
    grant = (
        await api.post(f"/api/v1/subscriptions/{subscription['id']}/grants", json={"protocol_id": protocol["id"]})
    ).json()
    job = await run_job(api, worker, await api.post(f"/api/v1/access/{grant['id']}/credentials"))
    return {"job": job, "grant": grant}


async def test_the_audit_of_an_assignment_has_no_keys_or_addresses(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    credential_id = await client(api, worker, gw, protocol, "f1")

    (entry,) = await world.audit(AuditAction.GATEWAY_CLIENT_ASSIGNED)

    assert entry.entity_id == UUID(credential_id)
    assert entry.meta == {"credential_id": credential_id, "slot": 1, "reason": "issue"}
    assert "10.77" not in json.dumps(entry.meta)


async def test_a_regular_protocol_client_is_not_touched_by_the_gateway_assignments(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    server, protocol, _ = await world.server()
    assert protocol is not None
    await world.issued(protocol)
    async with session_factory() as session:
        assert (await session.execute(select(GatewayAssignment))).first() is None


# --- отзыв и переиспользованный адрес ------------------------------------------------------------------------------


async def test_revoking_a_client_removes_its_rule(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    credential_id = await client(api, worker, gw, protocol, "g1")
    keep = await client(api, worker, gw, protocol, "g2")
    grant_id = await grant_of(credential_id)

    assert (await api.post(f"/api/v1/access/{grant_id}/revoke")).status_code == 200
    await drain(worker)

    assert [item["credential_id"] for item in await assignments(api, gw.sid)] == [keep]
    assert chain(gw) == {await address_of(keep): chain(gw)[await address_of(keep)]} and len(chain(gw)) == 1
    assert gw.gateway.files[ASSIGN_FILE].count("-A GW_ASSIGN") == 1


async def grant_of(credential_id: str) -> UUID:
    async with session_factory() as session:
        row = await session.get(VPNCredential, UUID(credential_id))
        assert row is not None
        return row.access_grant_id


async def test_a_reused_address_does_not_inherit_the_old_assignment(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, second) = await gateway_with_exits(api, worker, world)
    old = await client(api, worker, gw, protocol, "h1")
    old_address = await address_of(old)
    assert chain(gw) == {old_address: 1001}  # первый клиент - на первом выходе
    # Первый выход загружен предельно: следующий клиент (с тем же адресом) должен уйти на второй.
    assert (await api.patch(exits_url(gw.sid, first["id"]), json={"max_clients": 1})).status_code == 200
    assert (await api.post(f"/api/v1/access/{await grant_of(old)}/revoke")).status_code == 200
    await drain(worker)
    assert chain(gw) == {}

    reused = await client(api, worker, gw, protocol, "h2")

    assert await address_of(reused) == old_address  # адрес выдан заново
    (item,) = await assignments(api, gw.sid)
    assert item["credential_id"] == reused
    assert chain(gw) == {old_address: 1001 if item["slot"] == 1 else 1002}
    assert item["exit_id"] in (first["id"], second["id"])


async def test_sync_removes_assignments_of_revoked_clients_and_their_rules(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    credential_id = await client(api, worker, gw, protocol, "i1")
    async with session_factory() as session:  # отозван в обход задачи (например, завис отзыв)
        row = await session.get(VPNCredential, UUID(credential_id))
        assert row is not None
        from datetime import datetime, timezone

        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()

    await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert await assignments(api, gw.sid) == [] and chain(gw) == {}


# --- снятие выхода: перенос клиентов ------------------------------------------------------------------------------


async def test_removing_an_exit_moves_its_clients_to_the_other_one_before_the_tunnel_goes(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, second) = await gateway_with_exits(api, worker, world)
    for name in ("j1", "j2", "j3", "j4"):
        await client(api, worker, gw, protocol, name)
    assert sorted(i["slot"] for i in await assignments(api, gw.sid)) == [1, 1, 2, 2]

    job = await run_job(api, worker, await api.delete(exits_url(gw.sid, first["id"])))

    assert job["status"] == "success", job
    assert job["result"]["moved"] == 2 and job["result"]["unassigned"] == 0
    items = await assignments(api, gw.sid)
    assert [i["slot"] for i in items] == [2, 2, 2, 2] and sorted(i["reason"] for i in items).count("drain") == 2
    assert sorted(chain(gw).values()) == [1002] * 4
    assert gw.gateway.exit_interfaces == ["gwx2"]
    completed = (await world.audit(AuditAction.GATEWAY_EXIT_REMOVE_COMPLETED, gw.sid))[0]
    assert completed.meta["moved"] == 2 and completed.meta["unassigned"] == 0
    drained = [e for e in await world.audit(AuditAction.GATEWAY_CLIENT_ASSIGNED) if e.meta["reason"] == "drain"]
    assert len(drained) == 2 and all(e.meta["slot"] == 2 for e in drained)


async def test_removing_the_last_exit_leaves_clients_without_an_exit_and_without_internet(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (only,) = await gateway_with_exits(api, worker, world, count=1)
    credentials = [await client(api, worker, gw, protocol, name) for name in ("k1", "k2")]
    assert len(chain(gw)) == 2

    assert (await api.delete(exits_url(gw.sid, only["id"]))).status_code == 409  # без подтверждения нельзя
    job = await run_job(api, worker, await api.delete(exits_url(gw.sid, only["id"]), params={"force": "true"}))

    assert job["status"] == "success", job
    assert job["result"]["moved"] == 0 and job["result"]["unassigned"] == 2
    assert await assignments(api, gw.sid) == [] and chain(gw) == {}  # метки нет - kill-switch режет трафик
    completed = (await world.audit(AuditAction.GATEWAY_EXIT_REMOVE_COMPLETED, gw.sid))[0]
    assert completed.meta["unassigned"] == 2 and "10.77" not in json.dumps(completed.meta)
    async with session_factory() as session:  # клиенты живы: конфиги выданы, отозвано ничего
        rows = (
            await session.execute(select(VPNCredential).where(VPNCredential.id.in_(map(UUID, credentials))))
        ).scalars()
        assert all(row.revoked_at is None for row in rows)

    server, _, _ = await exit_server(world, "198.51.100.30")  # появился новый выход: клиенты получают его сами
    job = await run_job(
        api, worker, await api.post(f"/api/v1/servers/{gw.sid}/gateway/exits", json={"exit_server_id": str(server.id)})
    )
    assert job["result"]["assigned"] == 2 and len(chain(gw)) == 2


async def test_a_failed_removal_after_the_drain_does_not_return_the_clients(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, _) = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "l1")
    gw.gateway.exit_down_fails = {1}

    failed = await run_job(api, worker, await api.delete(exits_url(gw.sid, first["id"])))

    assert failed["status"] == "failed"
    assert [i["slot"] for i in await assignments(api, gw.sid)] == [2]  # перенос уже сохранён
    gw.gateway.exit_down_fails = set()
    again = await run_job(api, worker, await api.delete(exits_url(gw.sid, first["id"])))
    assert again["status"] == "success" and chain(gw) and set(chain(gw).values()) == {1002}


# --- sync ------------------------------------------------------------------------------------------------------------


async def test_sync_reapplies_the_map_when_the_gateway_differs_from_the_db(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "m1")
    await client(api, worker, gw, protocol, "m2")
    desired = chain(gw)
    wrong = {"10.77.0.99": 1002, **{k: 1002 for k in list(desired)[:1]}}  # чужая и неверная метки
    gw.gateway.assign_chain = dict(wrong)
    gw.gateway.files[ASSIGN_FILE] = render_assign_rules({IPv4Address(k): v - 1000 for k, v in wrong.items()})
    applies = gw.gateway.assign_applies

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert report["assignments"]["reapplied"] is True and report["assignments"]["assigned"] == 2
    assert any("re-applied" in issue for issue in report["issues"])
    assert chain(gw) == desired and gw.gateway.assign_applies == applies + 1
    assert "10.77" not in json.dumps(report["assignments"]) and "10.77" not in json.dumps(report["exits"])


async def test_sync_leaves_a_correct_map_alone(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "n1")
    applies = gw.gateway.assign_applies

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert report["assignments"] == {
        "assigned": 1,
        "newly_assigned": 0,
        "reapplied": False,
        "container_recreated": False,
    }
    assert gw.gateway.assign_applies == applies and report["issues"] == []


async def test_sync_restores_a_lost_file_so_the_map_survives_a_restart(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "o1")
    gw.gateway.files.pop(ASSIGN_FILE)

    await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert ASSIGN_FILE in gw.gateway.files and gw.gateway.files[ASSIGN_FILE].count("-A GW_ASSIGN") == 1


async def test_sync_assigns_clients_that_have_no_exit(api: httpx.AsyncClient, worker: JobWorker, world: World) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    credential_id = await client(api, worker, gw, protocol, "p1")
    async with session_factory() as session:
        row = await session.get(GatewayAssignment, UUID(credential_id))
        assert row is not None
        await session.delete(row)
        await session.commit()
    gw.gateway.assign_chain = {}

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert report["assignments"]["newly_assigned"] == 1
    (item,) = await assignments(api, gw.sid)
    assert item["reason"] == "sync" and len(chain(gw)) == 1


async def test_sync_recreates_an_exit_tunnel_raised_by_the_old_config_without_the_mark_rule(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world, count=1)
    path = exit_host_file(GATEWAY_LAYOUT, 1)
    old = "\n".join(line for line in gw.gateway.files[path].splitlines() if "ip rule" not in line) + "\n"
    gw.gateway.files[path] = gw.gateway.exit_configs["gwx1"] = old
    gw.gateway.ip_rules = [rule for rule in gw.gateway.ip_rules if "fwmark" not in rule]

    await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert "ip rule add" in gw.gateway.files[path]
    assert any("fwmark 0x3e9 lookup 1001" in rule for rule in gw.gateway.ip_rules)
    assert gw.gateway.exit_interfaces == ["gwx1"]


async def test_sync_repairs_a_gateway_with_an_old_start_script(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "q1")
    gw.gateway.firewall.discard("assign")  # цепочки GW_ASSIGN ещё нет
    gw.gateway.assign_chain = {}

    await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert "assign" in gw.gateway.firewall and len(chain(gw)) == 1


async def test_sync_survives_a_failing_tunnel_listing(
    api: httpx.AsyncClient, worker: JobWorker, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.ssh_exception import CommandExecutionError
    from infrastructure.vpn.gateway import AmneziaGatewayProvider

    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "r1")

    async def failing(self: AmneziaGatewayProvider, desired: Any) -> None:
        raise CommandExecutionError("Failed to apply the client assignments")

    monkeypatch.setattr(AmneziaGatewayProvider, "apply_assignments", failing)
    gw.gateway.files.pop(ASSIGN_FILE)  # файл потерян: карту надо применить заново, а применение падает
    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert sync["status"] == "success", sync  # сверка выходов не роняет весь sync
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert any("Failed to reconcile client assignments" in issue for issue in report["issues"])


# --- API -------------------------------------------------------------------------------------------------------------


async def test_assignments_api_shows_no_addresses_or_keys(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "s1")

    response = await api.get(f"/api/v1/servers/{gw.sid}/gateway/assignments")

    (item,) = response.json()
    assert set(item) == {"credential_id", "exit_id", "slot", "reason", "assigned_at"}
    assert "10.77" not in response.text and "PrivateKey" not in response.text


async def test_assignments_of_a_server_without_a_gateway_are_empty_and_of_a_missing_server_404(
    api: httpx.AsyncClient, world: World
) -> None:
    plain, _, _ = await world.server(install=False)
    assert (await api.get(f"/api/v1/servers/{plain.id}/gateway/assignments")).json() == []
    missing = "00000000-0000-4000-8000-000000000000"
    assert (await api.get(f"/api/v1/servers/{missing}/gateway/assignments")).status_code == 404


async def test_deleting_an_exit_row_drops_the_assignments_by_cascade(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, _) = await gateway_with_exits(api, worker, world)
    credential_id = await client(api, worker, gw, protocol, "t1")
    async with session_factory() as session:
        row = await session.get(GatewayExit, UUID(first["id"]))
        assert row is not None and row.state == ExitState.ACTIVE
        assert await session.get(GatewayAssignment, UUID(credential_id)) is not None
        await session.delete(row)
        await session.commit()
        assert await session.get(GatewayAssignment, UUID(credential_id)) is None


# --- ревью 24.3: готовность шлюза, перенос с повтором, подтверждение снятия, сбои ------------------------------------


def make_outdated(gw: Scene) -> None:
    """Шлюз, поставленный до 24.3: ни цепочки, ни прыжков, ни защиты FORWARD, прежние sysctl контейнера."""
    fake = gw.gateway
    fake.firewall -= {"assign", "assign_hooks", "ct_guard"}
    fake.assign_chain = {}
    name = GATEWAY_LAYOUT.container_name
    fake.container_sysctls[name] = [item.replace("rp_filter=0", "rp_filter=2") for item in fake.container_sysctls[name]]


async def test_sync_makes_an_old_gateway_ready_before_it_trusts_the_map_and_audits_the_recreation(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    credential_id = await client(api, worker, gw, protocol, "u1")
    make_outdated(gw)

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert report["assignments"]["container_recreated"] is True
    assert any("recreated" in issue for issue in report["issues"])
    assert {"assign", "assign_hooks", "ct_guard"} <= gw.gateway.firewall  # прыжки и защита на месте
    assert chain(gw) == {await address_of(credential_id): 1001}
    (entry,) = await world.audit(AuditAction.GATEWAY_CONTAINER_RECREATED, gw.sid)
    assert entry.meta == {"server_id": str(gw.sid), "reason": "sysctl_mismatch"}
    assert gw.gateway.exit_interfaces  # выходы вернулись из файлов


async def test_sync_does_not_trust_a_chain_created_without_hooks(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    """Цепочка GW_ASSIGN есть (её создал restore), а прыжков нет: прежняя проверка называла это «в порядке»."""
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "u2")
    gw.gateway.firewall.discard("assign_hooks")

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert "assign_hooks" in gw.gateway.firewall and sync["status"] == "success"
    assert len(await world.audit(AuditAction.GATEWAY_CONTAINER_RECREATED, gw.sid)) == 0  # контейнер цел


async def test_issuing_a_client_on_an_old_gateway_repairs_it_first(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    make_outdated(gw)

    credential_id = await client(api, worker, gw, protocol, "u3")

    assert "assign_hooks" in gw.gateway.firewall
    assert chain(gw) == {await address_of(credential_id): 1001}


async def test_a_sync_survives_a_gateway_container_that_cannot_be_recreated(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    make_outdated(gw)
    gw.gateway.docker_run_fails = 2

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert sync["status"] == "success", sync  # sync не падает целиком
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert any("Failed to reconcile client assignments" in issue and "again" in issue for issue in report["issues"])
    assert GATEWAY_LAYOUT.container_name not in gw.gateway.containers  # отсутствие контейнера не скрыто


async def test_a_drain_that_could_not_be_applied_changes_nothing_and_the_retry_repeats_it(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, _) = await gateway_with_exits(api, worker, world)
    for name in ("v1", "v2", "v3", "v4"):
        await client(api, worker, gw, protocol, name)
    assert sorted(i["slot"] for i in await assignments(api, gw.sid)) == [1, 1, 2, 2]
    gw.gateway.assign_apply_fails = True

    failed = await run_job(api, worker, await api.delete(exits_url(gw.sid, first["id"])))

    assert failed["status"] == "failed"
    assert sorted(i["slot"] for i in await assignments(api, gw.sid)) == [1, 1, 2, 2]  # БД не разошлась со шлюзом
    gw.gateway.assign_apply_fails = False

    again = await run_job(api, worker, await api.delete(exits_url(gw.sid, first["id"])))

    assert again["status"] == "success" and again["result"]["moved"] == 2
    assert set(chain(gw).values()) == {1002} and len(chain(gw)) == 4  # правил с меткой удалённого выхода не осталось
    completed = await world.audit(AuditAction.GATEWAY_EXIT_REMOVE_COMPLETED, gw.sid)
    assert completed[0].meta["moved"] == 2


async def test_the_counters_of_a_drain_survive_a_retry_after_a_later_step_failed(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, _) = await gateway_with_exits(api, worker, world)
    for name in ("w1", "w2"):
        await client(api, worker, gw, protocol, name)
    gw.gateway.exit_down_fails = {1}
    assert (await run_job(api, worker, await api.delete(exits_url(gw.sid, first["id"]))))["status"] == "failed"
    gw.gateway.exit_down_fails = set()

    again = await run_job(api, worker, await api.delete(exits_url(gw.sid, first["id"])))

    assert again["status"] == "success" and again["result"]["moved"] == 1 and again["result"]["unassigned"] == 0
    completed = await world.audit(AuditAction.GATEWAY_EXIT_REMOVE_COMPLETED, gw.sid)
    assert completed[0].meta["moved"] == 1


async def test_a_revoked_client_does_not_distort_the_drain(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, _) = await gateway_with_exits(api, worker, world)
    live = await client(api, worker, gw, protocol, "x1")
    dead = await client(api, worker, gw, protocol, "x2")
    async with session_factory() as session:  # отозван в обход задачи: назначение ещё в таблице
        from datetime import datetime, timezone

        row = await session.get(VPNCredential, UUID(dead))
        assert row is not None
        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()

    job = await run_job(api, worker, await api.delete(exits_url(gw.sid, first["id"])))

    assert job["status"] == "success" and job["result"]["moved"] <= 1
    assert [i["credential_id"] for i in await assignments(api, gw.sid)] == [live]


async def test_removing_the_last_exit_with_clients_needs_a_confirmation(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (only,) = await gateway_with_exits(api, worker, world, count=1)
    credentials = [await client(api, worker, gw, protocol, name) for name in ("y1", "y2")]

    refused = await api.delete(exits_url(gw.sid, only["id"]))

    assert refused.status_code == 409
    assert "2 client(s)" in refused.json()["detail"] and "force=true" in refused.json()["detail"]
    assert len(await assignments(api, gw.sid)) == 2 and (await list_exits(api, gw.sid))[0]["state"] == "active"
    assert await world.jobs(JobType.GATEWAY_REMOVE_EXIT) == []

    forced = await api.delete(exits_url(gw.sid, only["id"]), params={"force": "true"})

    assert forced.status_code == 202
    await drain(worker)
    assert await assignments(api, gw.sid) == [] and chain(gw) == {}
    events = await world.audit(AuditAction.GATEWAY_CLIENT_UNASSIGNED)
    assert sorted(entry.meta["credential_id"] for entry in events) == sorted(credentials)
    assert all(entry.meta == {"credential_id": str(entry.entity_id), "slot": 1} for entry in events)
    assert "10.77" not in json.dumps([entry.meta for entry in events])


async def test_removing_an_exit_without_clients_or_with_room_elsewhere_needs_no_confirmation(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, _) = await gateway_with_exits(api, worker, world)
    await client(api, worker, gw, protocol, "z1")
    assert (await api.delete(exits_url(gw.sid, first["id"]))).status_code == 202  # есть второй выход
    await drain(worker)
    empty, _, _ = await exit_server(world, "198.51.100.40")
    spare = await add_exit(api, worker, gw.sid, empty.id)
    assert (await api.delete(exits_url(gw.sid, spare["id"]))).status_code == 202  # на нём нет клиентов


async def test_not_enough_room_on_the_other_exits_also_needs_a_confirmation(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, (first, second) = await gateway_with_exits(api, worker, world)
    for name in ("q1", "q2"):
        await client(api, worker, gw, protocol, name)
    assert sorted(i["slot"] for i in await assignments(api, gw.sid)) == [1, 2]
    assert (await api.patch(exits_url(gw.sid, second["id"]), json={"max_clients": 1})).status_code == 200

    refused = await api.delete(exits_url(gw.sid, first["id"]))

    assert refused.status_code == 409 and "1 client(s)" in refused.json()["detail"]
    assert (await api.delete(exits_url(gw.sid, first["id"]), params={"force": "true"})).status_code == 202


async def test_invalid_assignments_are_skipped_and_do_not_break_the_map(
    api: httpx.AsyncClient, worker: JobWorker, world: World
) -> None:
    gw, protocol, _ = await gateway_with_exits(api, worker, world)
    good = await client(api, worker, gw, protocol, "r1")
    bad_outside = await client(api, worker, gw, protocol, "r2")
    bad_network = await client(api, worker, gw, protocol, "r3")
    duplicate = await client(api, worker, gw, protocol, "r4")
    good_address = await address_of(good)
    async with session_factory() as session:
        for credential_id, address in (
            (bad_outside, "192.0.2.9"),
            (bad_network, "10.77.0.0"),
            (duplicate, good_address),
        ):
            row = await session.get(VPNCredential, UUID(credential_id))
            assert row is not None
            row.client_address = address
        await session.commit()

    sync = await run_job(api, worker, await api.post(f"/api/v1/servers/{gw.sid}/sync"))

    assert sync["status"] == "success", sync
    report = next(item for item in sync["result"]["protocols"] if item["type"] == "amnezia_gateway")
    assert report["assignments"]["skipped"] == 3 and report["assignments"]["assigned"] == 1
    assert any("3 client assignment(s) were skipped" in issue for issue in report["issues"])
    assert set(chain(gw)) == {good_address}
    assert "192.0.2" not in json.dumps(report["assignments"]) and "192.0.2" not in " ".join(report["issues"])
