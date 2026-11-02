"""Событие `server_health_changed` (этап 25.5, Р9): пишется при любой смене `health_status`, в том же commit, что сама
смена; одно событие на смену, даже если две проверки идут одновременно; причина — только у `to=down`, без текста
исключения; имени сервера в событии нет."""

import json

import pytest

from core.database.postgres import session_factory
from core.exceptions import SSHAuthError, SSHHostKeyError, SSHTimeoutError
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.servers.health import ServerHealthService
from modules.servers.models.server import HealthStatus, VPNServer
from tests.bot_notify_support import HEALTH_CHANGED, admin_login, audit_rows
from tests.fakes.fake_ssh import FakeSSHClient
from tests.fixtures import World


async def check(world: World, server_id, actor: AuditActor = SYSTEM_ACTOR):  # type: ignore[no-untyped-def]
    return await ServerHealthService(world.session, actor).check(server_id)


async def metas() -> list[dict]:  # type: ignore[type-arg]
    return [row[3] for row in await audit_rows(HEALTH_CHANGED)]


async def test_the_first_check_records_a_change_from_nothing(world: World) -> None:
    server, _, _ = await world.server()

    await check(world, server.id)

    (row,) = await audit_rows(HEALTH_CHANGED)
    assert row[1:3] == ("server", server.id)
    assert row[3] == {"from": None, "to": "ok"}


async def test_a_repeated_check_with_the_same_status_writes_nothing(world: World) -> None:
    server, _, _ = await world.server()

    await check(world, server.id)
    await check(world, server.id)
    await check(world, server.id)

    assert len(await audit_rows(HEALTH_CHANGED)) == 1


async def test_going_down_records_unreachable(world: World) -> None:
    server, _, fake = await world.server()
    await check(world, server.id)
    fake.reachable = False

    await check(world, server.id)

    assert (await metas())[-1] == {"from": "ok", "to": "down", "reason": "unreachable"}


async def test_a_second_down_check_is_silent(world: World) -> None:
    server, _, fake = await world.server()
    await check(world, server.id)
    fake.reachable = False
    await check(world, server.id)

    await check(world, server.id)

    assert len(await audit_rows(HEALTH_CHANGED)) == 2


async def test_recovery_has_no_reason(world: World) -> None:
    server, _, fake = await world.server()
    fake.reachable = False
    await check(world, server.id)
    fake.reachable = True

    await check(world, server.id)

    assert (await metas())[-1] == {"from": "down", "to": "ok"}


async def test_degraded_is_also_a_recorded_change_without_reason(world: World) -> None:
    server, protocol, fake = await world.server()
    await check(world, server.id)
    fake.nat_rule = False
    protocol.port = 1
    await world.session.commit()

    await check(world, server.id)

    assert (await metas())[-1] == {"from": "ok", "to": "degraded"}


async def test_a_timeout_is_unreachable(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    server, _, _ = await world.server()
    await check(world, server.id)

    async def timeout(self: FakeSSHClient) -> None:
        raise SSHTimeoutError("timed out")

    monkeypatch.setattr(FakeSSHClient, "connect", timeout)
    await check(world, server.id)

    assert (await metas())[-1]["reason"] == "unreachable"


@pytest.mark.parametrize(
    "error",
    [SSHAuthError("denied"), SSHHostKeyError("203.0.113.10", "SHA256:expected", "SHA256:presented")],
    ids=["login", "host-key"],
)
async def test_a_login_or_host_key_failure_is_ssh_error(
    world: World, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    server, _, _ = await world.server()
    await check(world, server.id)

    async def refuse(self: FakeSSHClient) -> None:
        raise error

    monkeypatch.setattr(FakeSSHClient, "connect", refuse)
    await check(world, server.id)

    assert (await metas())[-1] == {"from": "ok", "to": "down", "reason": "ssh_error"}


async def test_the_exception_text_never_reaches_the_event(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    server, _, _ = await world.server()

    async def refuse(self: FakeSSHClient) -> None:
        raise SSHAuthError("password=Hunter2-secret for root@203.0.113.10")

    monkeypatch.setattr(FakeSSHClient, "connect", refuse)
    await check(world, server.id)

    dump = json.dumps(await audit_rows(HEALTH_CHANGED), default=str)
    assert "Hunter2" not in dump and "203.0.113.10" not in dump and server.name not in dump


async def test_the_event_is_attributed_to_the_actor_of_the_check(world: World) -> None:
    server, _, fake = await world.server()
    admin = await admin_login()
    await check(world, server.id, AuditActor(admin_id=admin.admin.id))
    fake.reachable = False

    await check(world, server.id, SYSTEM_ACTOR)

    first, second = await audit_rows(HEALTH_CHANGED)
    assert first[0] == admin.admin.id and second[0] is None


async def test_the_event_and_the_status_are_saved_together(world: World) -> None:
    server, _, fake = await world.server()
    fake.reachable = False

    await check(world, server.id)

    async with session_factory() as other:
        stored = await other.get(VPNServer, server.id)
        assert stored is not None and stored.health_status == HealthStatus.DOWN
    assert len(await audit_rows(HEALTH_CHANGED)) == 1


async def test_two_checks_with_a_stale_view_of_the_status_write_one_event(world: World) -> None:
    """Вторая проверка уже держит в памяти старый статус (None): прежний статус перечитывается под FOR UPDATE, поэтому
    событие «None -> ok» второй раз не пишется."""
    server, _, _ = await world.server()
    async with session_factory() as late:
        stale = await late.get(VPNServer, server.id)
        assert stale is not None and stale.health_status is None

        await check(world, server.id)  # первая проверка закоммитила None -> ok
        await ServerHealthService(late, SYSTEM_ACTOR).check(server.id)  # вторая шла с устаревшим представлением

    assert len(await audit_rows(HEALTH_CHANGED)) == 1


async def test_a_stale_check_does_not_hide_a_real_change(world: World) -> None:
    server, _, fake = await world.server()
    await check(world, server.id)
    async with session_factory() as late:
        stale = await late.get(VPNServer, server.id)
        assert stale is not None and stale.health_status == HealthStatus.OK
        fake.reachable = False
        await check(world, server.id)  # ok -> down
        fake.reachable = True
        await ServerHealthService(late, SYSTEM_ACTOR).check(server.id)  # видит down в БД: down -> ok

    # created_at is the transaction start: the stale transaction's recovery can sort before down.
    changes = [(m["from"], m["to"]) for m in await metas()]
    assert len(changes) == 3
    assert set(changes) == {(None, "ok"), ("ok", "down"), ("down", "ok")}
