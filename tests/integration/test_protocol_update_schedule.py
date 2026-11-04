"""Daily calendar rule uses Moscow 03:00 and Redis date dedup, never a 24h startup interval."""

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from core.config import settings
from infrastructure.workers import scheduler
from tests.fixtures import World


def daily() -> Any:
    method = getattr(scheduler, "check_protocol_updates_daily", None)
    assert callable(method), "Daily protocol availability checks must follow calendar 03:00 Europe/Moscow"
    return method


def test_daily_check_is_wired_into_existing_scheduler() -> None:
    method = daily()
    rules = {rule.name: rule for rule in scheduler.build_rules(settings)}
    assert "protocol_updates" in rules
    assert rules["protocol_updates"].action is method
    assert 0 < rules["protocol_updates"].interval <= 60


async def test_moscow_0300_boundary_and_calendar_date_dedup_across_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    method = daily()
    calls: list[str] = []

    async def collect() -> str:
        calls.append("checked")
        return "checked 0 protocols"

    monkeypatch.setattr(scheduler, "collect_protocol_updates", collect, raising=False)
    await method(now=datetime(2026, 11, 3, 23, 59, 59, tzinfo=timezone.utc))  # 02:59:59 MSK
    assert calls == []
    await method(now=datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc))  # 03:00 MSK
    assert calls == ["checked"]
    # Another worker/restart later that day must not scan again.
    await method(now=datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc))
    assert calls == ["checked"]
    await method(now=datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc))
    assert calls == ["checked", "checked"]


async def test_starting_late_catches_up_once_for_today_without_replaying_previous_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    method = daily()
    calls: list[str] = []

    async def collect() -> str:
        calls.append("checked")
        return "checked 0 protocols"

    monkeypatch.setattr(scheduler, "collect_protocol_updates", collect, raising=False)
    await method(now=datetime(2026, 11, 4, 18, 15, tzinfo=timezone.utc))  # restart at21:15 MSK
    await method(now=datetime(2026, 11, 4, 18, 16, tzinfo=timezone.utc))
    assert calls == ["checked"]


async def test_infrastructure_failure_releases_date_claim_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    method = daily()
    calls: list[str] = []

    async def collect() -> str:
        calls.append("attempt")
        if len(calls) == 1:
            raise RedisConnectionError("synthetic collector infrastructure failure")
        return "checked 0 protocols"

    monkeypatch.setattr(scheduler, "collect_protocol_updates", collect, raising=False)
    moment = datetime(2026, 11, 4, 0, 5, tzinfo=timezone.utc)
    try:
        await method(now=moment)
    except RedisConnectionError:
        pass  # Either surfacing or reporting the failure is fine; it must release its claim.
    await method(now=moment)
    await method(now=moment)
    assert calls == ["attempt", "attempt"]


async def test_concurrent_daily_ticks_do_not_duplicate_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    method = daily()
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def collect() -> str:
        calls.append("scan")
        started.set()
        await release.wait()
        return "checked 0 protocols"

    monkeypatch.setattr(scheduler, "collect_protocol_updates", collect, raising=False)
    moment = datetime(2026, 11, 4, 0, 5, tzinfo=timezone.utc)
    first = asyncio.create_task(method(now=moment))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(method(now=moment), timeout=2)
        assert calls == ["scan"]
    finally:
        release.set()
        await first


async def test_collector_isolates_one_host_failure_and_never_schedules_container_updates(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from modules.jobs.models.job import Job, JobType
    from modules.servers.protocol_updates import ProtocolUpdateCheckService

    first, _, _ = await world.server()
    second, _, _ = await world.server()
    broken_id, healthy_id = first.id, second.id
    calls: list[object] = []
    sessions: list[object] = []

    async def check(self: Any, server_id: object) -> dict[str, Any]:
        calls.append(server_id)
        sessions.append(self.session)
        if server_id == broken_id:
            raise RuntimeError("synthetic host error")
        return {"protocols": []}

    monkeypatch.setattr(ProtocolUpdateCheckService, "check", check)
    await scheduler.collect_protocol_updates()
    assert set(calls) == {broken_id, healthy_id}
    assert len({id(session) for session in sessions}) == 2
    jobs = (await world.session.execute(select(Job).where(Job.type == JobType.UPDATE_PROTOCOL))).scalars().all()
    assert jobs == []
