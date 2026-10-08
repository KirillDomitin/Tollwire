from dataclasses import replace

import pytest

from infrastructure.monitoring.host_metrics import HostCounters, compute_rates, parse_host_metrics

# Вывод HOST_METRICS_COMMAND на типичном VPS (числа правдоподобные, не с реального сервера).
OUTPUT = """@uptime
86400.50 170000.00
@stat
cpu  1000 0 500 8000 500 0 0 0 0 0
@loadavg
0.42 0.30 0.25 1/150 12345
@meminfo
MemTotal:        2000000 kB
MemAvailable:    1500000 kB
@df
Filesystem     1-blocks       Used  Available Capacity Mounted on
/dev/vda1   42000000000 21000000000 19000000000      53% /
@route
default via 203.0.113.1 dev eth0 proto dhcp src 203.0.113.20 metric 100
@netdev
Inter-|   Receive                                                |  Transmit
 face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed
    lo: 5000 50 0 0 0 0 0 0 5000 50 0 0 0 0 0 0
  eth0: 9000000 9000 0 0 0 0 0 0 4000000 4000 0 0 0 0 0 0
docker0: 777777 1000 0 0 0 0 0 0 888888 1000 0 0 0 0 0 0
@diskstats
 253       0 vda 1000 0 20000 500 2000 0 40000 900 0 1000 1400 0 0 0 0
 253       1 vda1 900 0 18000 450 1900 0 38000 850 0 950 1300 0 0 0 0
   7       0 loop0 50 0 400 10 0 0 0 0 0 10 10 0 0 0 0
"""


def test_parses_load_memory_disk_and_counters() -> None:
    sample = parse_host_metrics(OUTPUT)

    assert sample.load1 == 0.42
    assert sample.mem_total_bytes == 2_000_000 * 1024
    assert sample.mem_used_bytes == 500_000 * 1024  # всего − доступно
    assert (sample.disk_total_bytes, sample.disk_used_bytes) == (42_000_000_000, 21_000_000_000)
    assert sample.counters.uptime_seconds == 86400.5
    assert sample.counters.cpu_total == 10_000  # user..steal
    assert sample.counters.cpu_idle == 8_500  # idle + iowait


def test_network_counters_come_from_the_default_route_interface() -> None:
    sample = parse_host_metrics(OUTPUT)
    assert sample.net_interface == "eth0"  # не lo и не docker0
    assert (sample.counters.net_rx_bytes, sample.counters.net_tx_bytes) == (9_000_000, 4_000_000)


def test_without_a_route_the_busiest_real_interface_is_used() -> None:
    output = OUTPUT.replace("default via 203.0.113.1 dev eth0 proto dhcp src 203.0.113.20 metric 100\n", "")
    assert parse_host_metrics(output).net_interface == "eth0"  # docker0 служебный, хоть и не пустой


def test_disk_io_counts_whole_disks_only() -> None:
    sample = parse_host_metrics(OUTPUT)
    # Только vda: раздел vda1 — часть того же диска, loop0 — не диск.
    assert sample.counters.disk_read_bytes == 20_000 * 512
    assert sample.counters.disk_written_bytes == 40_000 * 512


def test_unexpected_output_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unexpected"):
        parse_host_metrics("@uptime\nnot-a-number\n")


COUNTERS = HostCounters(
    uptime_seconds=1000.0,
    cpu_total=10_000,
    cpu_idle=8_000,
    net_rx_bytes=1_000_000,
    net_tx_bytes=500_000,
    disk_read_bytes=0,
    disk_written_bytes=0,
)


def test_rates_are_deltas_over_server_uptime() -> None:
    later = replace(
        COUNTERS,
        uptime_seconds=1060.0,  # прошла минута по часам сервера
        cpu_total=16_000,
        cpu_idle=12_500,  # из 6000 тиков 4500 простоя → занято 25 %
        net_rx_bytes=1_000_000 + 60 * 125_000,  # 1 Мбит/с
        net_tx_bytes=500_000 + 60 * 12_500,
        disk_written_bytes=60 * 4096,
    )
    rates = compute_rates(COUNTERS, later)
    assert rates is not None
    assert rates.cpu_percent == 25.0
    assert rates.net_rx_bytes_per_s == 125_000
    assert rates.net_tx_bytes_per_s == 12_500
    assert rates.disk_write_bytes_per_s == 4096 and rates.disk_read_bytes_per_s == 0


def test_reboot_or_counter_reset_gives_no_rates() -> None:
    rebooted = replace(COUNTERS, uptime_seconds=30.0, cpu_total=200, cpu_idle=150)
    assert compute_rates(COUNTERS, rebooted) is None  # аптайм уменьшился
    reset = replace(COUNTERS, uptime_seconds=1060.0, cpu_total=16_000, cpu_idle=12_000, net_rx_bytes=10)
    assert compute_rates(COUNTERS, reset) is None  # счётчик сети сбросился — не рисуем отрицательный выброс
