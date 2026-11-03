"""Daily calendar rule uses Moscow 03:00 and Redis date dedup, never a 24h startup interval."""

from datetime import datetime, timezone
from typing import Any

import pytest

from core.config import settings
from infrastructure.workers import scheduler


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
