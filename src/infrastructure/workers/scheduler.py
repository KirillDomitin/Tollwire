#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from core.config import Settings
from core.database.postgres import session_factory
from core.redis import get_redis
from modules.access.service import SubscriptionService
from modules.audit.actor import SYSTEM_ACTOR
from modules.auth.service import AuthService
from modules.jobs.service import JobService
from modules.servers.maintenance import ServerMaintenanceService


@dataclass(frozen=True)
class Rule:
    """Периодическое правило: имя, интервал в секундах (0 — выключено) и действие, возвращающее сводку."""

    name: str
    interval: int
    action: Callable[[], Awaitable[str]]


class Scheduler:
    """Планировщик внутри воркера: раз в tick проверяет, каким правилам пора выполниться.

    «Пора» решает Redis: SET scheduler:rule:<имя> NX EX <интервал>. Ключ ставит только первый
    успевший процесс и живёт ровно интервал — так при нескольких воркерах правило выполняется
    один раз за интервал, а не N раз. Все правила идемпотентны, поэтому редкий повтор безопасен.
    """

    def __init__(self, rules: Sequence[Rule], tick_seconds: int):
        self.rules = [rule for rule in rules if rule.interval > 0]
        self.tick_seconds = tick_seconds
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.run_due()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass

    async def run_due(self) -> None:
        for rule in self.rules:
            if self._stop.is_set():
                return
            if not await self._claim(rule):
                continue
            try:
                print(f"[scheduler] {rule.name}: {await rule.action()}")
            except Exception as exc:
                # Сбой одного правила не должен останавливать остальные и сам цикл.
                print(f"[scheduler] {rule.name} failed: {exc}")

    @staticmethod
    async def _claim(rule: Rule) -> bool:
        return bool(await get_redis().set(f"scheduler:rule:{rule.name}", "1", nx=True, ex=rule.interval))


async def _for_each_subscription(
    list_ids: Callable[[SubscriptionService], Awaitable[Sequence[UUID]]],
    apply: Callable[[SubscriptionService, UUID], Awaitable[object]],
) -> tuple[int, int]:
    async with session_factory() as session:
        ids = await list_ids(SubscriptionService(session, SYSTEM_ACTOR))
    done = 0
    for subscription_id in ids:
        # Своя сессия на каждую подписку: ошибка в одной не откатывает и не блокирует остальные.
        try:
            async with session_factory() as session:
                await apply(SubscriptionService(session, SYSTEM_ACTOR), subscription_id)
            done += 1
        except Exception as exc:
            print(f"[scheduler] subscription {subscription_id} failed: {exc}")
    return done, len(ids)


async def apply_access_rules() -> str:
    """Истечение (EXPIRED + отзыв клиентов на серверах) и активация (PENDING → ACTIVE) подписок."""
    expired = await _for_each_subscription(lambda s: s.list_due_for_expiry(), lambda s, i: s.expire(i))
    activated = await _for_each_subscription(lambda s: s.list_due_for_activation(), lambda s, i: s.activate(i))
    return f"expired {expired[0]}/{expired[1]}, activated {activated[0]}/{activated[1]}"


async def cleanup_admin_sessions() -> str:
    async with session_factory() as session:
        deleted = await AuthService(session, SYSTEM_ACTOR).cleanup_expired_sessions()
    return f"deleted {deleted} expired admin sessions"


async def schedule_health_checks() -> str:
    async with session_factory() as session:
        jobs = await ServerMaintenanceService(session).schedule_health_checks()
    return f"queued {len(jobs)} health checks"


async def schedule_syncs() -> str:
    async with session_factory() as session:
        jobs = await ServerMaintenanceService(session).schedule_syncs()
    return f"queued {len(jobs)} syncs"


async def fail_stale_jobs(max_age_seconds: int) -> str:
    async with session_factory() as session:
        failed = await JobService(session, SYSTEM_ACTOR).fail_stale_running(max_age_seconds)
    return f"marked {failed} stale running jobs as failed"


def build_rules(settings: Settings) -> list[Rule]:
    return [
        Rule("access", settings.access_rules_interval_seconds, apply_access_rules),
        Rule("admin_sessions", settings.session_cleanup_interval_seconds, cleanup_admin_sessions),
        Rule(
            "stale_jobs",
            settings.stale_jobs_interval_seconds,
            lambda: fail_stale_jobs(settings.stale_job_timeout_seconds),
        ),
        Rule("health", settings.health_interval_seconds, schedule_health_checks),
        Rule("sync", settings.sync_interval_seconds, schedule_syncs),
    ]
