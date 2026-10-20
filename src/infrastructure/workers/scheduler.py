#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from core.config import Settings
from core.database.postgres import session_factory
from core.exceptions import JobAlreadyRunningError
from core.log import bind
from core.redis import get_redis
from modules.access.service import SubscriptionService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.service import AuditService
from modules.auth.service import AuthService
from modules.gateway.failover import GatewayFailoverService
from modules.geoip.service import GeoipService
from modules.jobs.service import JobService
from modules.monitoring.service import MetricsCollector, rollup_and_prune
from modules.protocols.models.protocol import ProtocolType
from modules.protocols.repository import ProtocolRepository
from modules.servers.maintenance import ServerMaintenanceService

logger = logging.getLogger("scheduler")


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
            try:
                await self.run_due()
            except Exception as exc:
                # _claim идёт в Redis вне защиты правил: сбой Redis не должен ронять процесс воркера.
                logger.warning("scheduler tick failed", extra={"error": str(exc)})
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
            with bind(rule=rule.name):
                try:
                    logger.info("rule done", extra={"summary": await rule.action()})
                except Exception:
                    # Сбой одного правила не должен останавливать остальные и сам цикл.
                    logger.exception("rule failed")

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
        except Exception:
            logger.exception("subscription rule failed", extra={"subscription_id": str(subscription_id)})
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


async def schedule_geoip_update() -> str:
    """Раз в неделю обновить российский список (этап 24.4), но только если есть хотя бы один шлюз: без шлюзов список
    не нужен, а лишний поход на RIPE не делаем."""
    async with session_factory() as session:
        if not await ProtocolRepository(session).list_by_type(ProtocolType.AMNEZIA_GATEWAY):
            return "no gateways, geoip update skipped"
        try:
            await GeoipService(session, SYSTEM_ACTOR).schedule_update()
        except JobAlreadyRunningError:
            return "geoip update is already running"
    return "queued geoip update"


WATCH_CONCURRENCY = 5  # шлюзов, опрашиваемых одновременно
WATCH_TIMEOUT_SECONDS = 20  # на один шлюз целиком (SSH, чтение, запись счётчиков, постановка задачи)


async def _watch_one(protocol_id: UUID, server_id: UUID, semaphore: asyncio.Semaphore) -> tuple[int, int, int]:
    """Проверка одного шлюза со своей сессией, под семафором и с общим сроком. Возвращает (проверен, поставлена задача,
    недоступен). Ошибка или срок одного шлюза не влияют на остальных и не меняют его счётчики (запись - в конце
    одной транзакции `check`: отмена до commit откатывает её)."""
    async with semaphore:
        try:
            async with session_factory() as session:
                gateway = await ProtocolRepository(session).get_by_id(protocol_id)
                if gateway is None:
                    return 0, 0, 0
                result = await asyncio.wait_for(
                    GatewayFailoverService(session, SYSTEM_ACTOR).watch(gateway), WATCH_TIMEOUT_SECONDS
                )
        except Exception as exc:  # noqa: BLE001 - один недоступный шлюз не должен останавливать остальные
            logger.warning(
                "gateway watch failed",
                extra={"server_id": str(server_id), "error": type(exc).__name__ + ": " + str(exc)[:200]},
            )
            return 0, 0, 1
    return int(result.checked), int(result.queued), 0


async def watch_gateways() -> str:
    """Проверка выходов шлюзов (этап 24.5): раз в минуту читает туннели (SSH, только чтение), ведёт счётчики отказов и
    ставит GATEWAY_APPLY, когда выход отказал / восстановился или есть кого переносить. Шлюзы без выходов не
    опрашиваются.
    Шлюзы опрашиваются параллельно (до `WATCH_CONCURRENCY`), на каждый - не больше `WATCH_TIMEOUT_SECONDS`: недоступные
    шлюзы не задерживают остальные правила планировщика."""
    async with session_factory() as session:
        gateways = [
            (item.id, item.server_id)
            for item in await ProtocolRepository(session).list_by_type(ProtocolType.AMNEZIA_GATEWAY)
        ]
    semaphore = asyncio.Semaphore(WATCH_CONCURRENCY)
    outcomes = await asyncio.gather(
        *(_watch_one(protocol_id, server_id, semaphore) for protocol_id, server_id in gateways)
    )
    checked = sum(item[0] for item in outcomes)
    queued = sum(item[1] for item in outcomes)
    failed = sum(item[2] for item in outcomes)
    return f"checked {checked} gateways, queued {queued} applies, {failed} unreachable"


async def fail_stale_jobs(max_age_seconds: int) -> str:
    async with session_factory() as session:
        failed = await JobService(session, SYSTEM_ACTOR).fail_stale_running(max_age_seconds)
    return f"marked {failed} stale running jobs as failed"


async def requeue_lost_jobs(max_age_seconds: int) -> str:
    async with session_factory() as session:
        requeued = await JobService(session, SYSTEM_ACTOR).requeue_lost_pending(max_age_seconds)
    return f"requeued {requeued} lost pending jobs"


async def apply_retention(job_days: int, audit_days: int) -> str:
    async with session_factory() as session:
        jobs = await JobService(session, SYSTEM_ACTOR).purge_finished(job_days)
    async with session_factory() as session:
        logs = await AuditService(session, SYSTEM_ACTOR).purge(audit_days)
    return f"deleted {jobs} finished jobs, {logs} audit records"


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
        Rule(
            "requeue_jobs",
            settings.requeue_interval_seconds,
            lambda: requeue_lost_jobs(settings.requeue_pending_after_seconds),
        ),
        Rule(
            "retention",
            settings.retention_interval_seconds,
            lambda: apply_retention(settings.job_retention_days, settings.audit_retention_days),
        ),
        # Мониторинг: замер серверов (только чтение по SSH) и свёртка истории по часам.
        Rule("geoip", settings.geoip_update_interval_seconds, schedule_geoip_update),
        Rule("gateway_watch", settings.gateway_watch_interval_seconds, watch_gateways),
        Rule("metrics", settings.metrics_interval_seconds, MetricsCollector().collect_all),
        Rule("metrics_rollup", settings.metrics_rollup_interval_seconds, rollup_and_prune),
    ]
