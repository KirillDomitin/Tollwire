"""Мониторинг: замеры с фейкового сервера, скорости по разнице счётчиков, свёртка по часам, чтение для API."""

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from modules.monitoring.models.metrics import ServerMetric, ServerMetricHourly
from modules.monitoring.schemas import MetricsRange
from modules.monitoring.service import MetricsCollector, MetricsService, rollup_and_prune
from tests.fixtures import World


async def samples(world: World) -> list[ServerMetric]:
    world.session.expire_all()
    return list((await world.session.execute(select(ServerMetric).order_by(ServerMetric.ts))).scalars())


async def test_first_sample_stores_counters_and_the_second_gives_rates(world: World) -> None:
    server, protocol, fake = await world.server()
    server_id = server.id  # expire_all() ниже не даст прочитать server.id без запроса к БД
    assert protocol is not None
    _, _, _, online = await world.issued(protocol)
    await world.issued(protocol)
    fake.handshakes[online.client_public_key] = int(time.time()) - 30  # был на связи полминуты назад

    assert await MetricsCollector().collect(server_id)
    (first,) = await samples(world)
    assert first.cpu_percent is None and first.net_rx_bytes_per_s is None  # сравнивать пока не с чем
    assert (first.clients_total, first.clients_online) == (2, 1)
    assert first.mem_used_bytes == 400_000 * 1024 and first.disk_used_bytes == 4_000_000_000

    # Через минуту по часам сервера: CPU занят на 25 %, по сети пришло 1000 байт/с, записано 4 КБ/с.
    fake.host_stats.update(
        uptime=fake.host_stats["uptime"] + 60,
        cpu_busy=fake.host_stats["cpu_busy"] + 1500,
        cpu_idle=fake.host_stats["cpu_idle"] + 4500,
        rx=fake.host_stats["rx"] + 60_000,
        written_sectors=60 * 8,
    )
    assert await MetricsCollector().collect(server_id)
    _, second = await samples(world)
    assert second.cpu_percent == 25.0
    assert second.net_rx_bytes_per_s == 1000 and second.net_tx_bytes_per_s == 0
    assert second.disk_write_bytes_per_s == 60 * 8 * 512 / 60


async def test_unreachable_server_is_a_gap_not_an_error(world: World) -> None:
    server, _, fake = await world.server()
    fake.reachable = False
    assert await MetricsCollector().collect(server.id) is False
    assert await samples(world) == []


async def test_server_without_protocol_has_host_metrics_but_no_client_counts(world: World) -> None:
    server, _, _ = await world.server(install=False)
    assert await MetricsCollector().collect(server.id)
    (sample,) = await samples(world)
    assert sample.clients_total is None and sample.load1 == 0.10


async def test_rollup_keeps_average_and_peak_and_api_switches_resolution(world: World) -> None:
    server, _, fake = await world.server()
    server_id = server.id
    assert await MetricsCollector().collect(server_id)
    for busy in (1500, 4500):  # 25 %, затем 75 %
        fake.host_stats.update(
            uptime=fake.host_stats["uptime"] + 60,
            cpu_busy=fake.host_stats["cpu_busy"] + busy,
            cpu_idle=fake.host_stats["cpu_idle"] + 6000 - busy,
        )
        assert await MetricsCollector().collect(server_id)

    await rollup_and_prune()
    rows = list((await world.session.execute(select(ServerMetricHourly))).scalars())
    assert sum(row.samples for row in rows) == 3
    assert max(row.cpu_percent_max or 0 for row in rows) == 75.0  # пик не «размазан» средним
    await rollup_and_prune()  # повторная свёртка перезаписывает, а не удваивает
    world.session.expire_all()
    assert sum(row.samples for row in (await world.session.execute(select(ServerMetricHourly))).scalars()) == 3

    service = MetricsService(world.session)
    day = await service.series(server_id, MetricsRange.DAY)
    assert day.resolution == "minute" and len(day.points) == 3
    assert [point.cpu_percent for point in day.points] == [None, 25.0, 75.0]
    week = await service.series(server_id, MetricsRange.WEEK)
    assert week.resolution == "hour" and week.points and week.points[-1].cpu_percent_max == 75.0
    (latest,) = await service.latest()
    assert latest.server_id == server_id and latest.cpu_percent == 75.0


async def test_old_history_is_pruned(world: World) -> None:
    server, _, _ = await world.server()
    assert await MetricsCollector().collect(server.id)
    (sample,) = await samples(world)
    sample.ts = datetime.now(timezone.utc) - timedelta(days=8)  # старше 7 дней хранения
    await world.session.commit()

    summary = await rollup_and_prune()
    assert "pruned 1 samples" in summary
    assert await samples(world) == []
