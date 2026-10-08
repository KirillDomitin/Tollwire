#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Системные метрики Linux-сервера без агента: одна фиксированная команда только для чтения по SSH.

Скорости (CPU, сеть, диск) из одного замера не получить — в /proc лежат накопительные счётчики. Поэтому замер
хранит счётчики, а скорость = разница счётчиков двух замеров / разница аптайма сервера. Аптайм — часы самого
сервера: интервал между замерами не обязан быть ровно минутой, а перезагрузку видно по уменьшению аптайма.
"""

import re
from dataclasses import dataclass

from infrastructure.ssh.client import SSHClient

# Только чтение, без пользовательского ввода. Разделы помечены строками «@имя» — так один вызов SSH отдаёт всё.
HOST_METRICS_COMMAND = (
    "echo @uptime; cat /proc/uptime; "
    "echo @stat; head -n 1 /proc/stat; "
    "echo @loadavg; cat /proc/loadavg; "
    "echo @meminfo; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; "
    "echo @df; df -P -B1 /; "
    "echo @route; ip -o route show default 2>/dev/null; "
    "echo @netdev; cat /proc/net/dev; "
    "echo @diskstats; cat /proc/diskstats"
)

SECTOR_BYTES = 512  # /proc/diskstats всегда считает в 512-байтных секторах
# Целые диски (не разделы, не loop/ram/device-mapper): их счётчики не дублируют друг друга.
_WHOLE_DISK = re.compile(r"(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+|mmcblk\d+)")
# Если маршрута по умолчанию не видно — интерфейс с наибольшим трафиком, кроме служебных.
_SERVICE_INTERFACE = re.compile(r"(lo|docker\d*|br-.*|veth.*|awg\d*|wg\d*|tun\d*|virbr\d*)")


@dataclass(frozen=True)
class HostCounters:
    """Накопительные счётчики: из пары таких замеров считаются скорости."""

    uptime_seconds: float
    cpu_total: int
    cpu_idle: int
    net_rx_bytes: int
    net_tx_bytes: int
    disk_read_bytes: int
    disk_written_bytes: int


@dataclass(frozen=True)
class HostSample:
    counters: HostCounters
    load1: float
    mem_total_bytes: int
    mem_used_bytes: int
    disk_total_bytes: int
    disk_used_bytes: int
    net_interface: str | None


@dataclass(frozen=True)
class HostRates:
    cpu_percent: float
    net_rx_bytes_per_s: float
    net_tx_bytes_per_s: float
    disk_read_bytes_per_s: float
    disk_write_bytes_per_s: float


def _sections(output: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in output.splitlines():
        if line.startswith("@") and line[1:].isidentifier():
            current = sections.setdefault(line[1:], [])
        elif current is not None and line.strip():
            current.append(line)
    return sections


def _main_interface(route_lines: list[str], traffic: dict[str, tuple[int, int]]) -> str | None:
    for line in route_lines:
        match = re.search(r"\bdev\s+(\S+)", line)
        if match and match.group(1) in traffic:
            return match.group(1)
    candidates = [name for name in traffic if not _SERVICE_INTERFACE.fullmatch(name)]
    return max(candidates, key=lambda name: sum(traffic[name]), default=None)


def parse_host_metrics(output: str) -> HostSample:
    """Разбирает вывод HOST_METRICS_COMMAND. ValueError — если чего-то обязательного нет (не тот сервер/ОС)."""
    sections = _sections(output)
    try:
        uptime = float(sections["uptime"][0].split()[0])

        cpu = [int(value) for value in sections["stat"][0].split()[1:9]]  # user..steal; guest уже внутри user
        cpu_total, cpu_idle = sum(cpu), cpu[3] + cpu[4]  # простой = idle + iowait

        load1 = float(sections["loadavg"][0].split()[0])

        memory = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in sections["meminfo"]}
        mem_total = memory["MemTotal"]
        mem_used = mem_total - memory["MemAvailable"]

        df_fields = sections["df"][-1].split()
        disk_total, disk_used = int(df_fields[1]), int(df_fields[2])
    except (KeyError, IndexError, ValueError):
        raise ValueError("Unexpected host metrics output") from None

    traffic: dict[str, tuple[int, int]] = {}
    for line in sections.get("netdev", []):
        if ":" not in line:
            continue  # две строки заголовка
        name, data = line.split(":", 1)
        fields = data.split()
        if len(fields) >= 9:
            traffic[name.strip()] = (int(fields[0]), int(fields[8]))
    interface = _main_interface(sections.get("route", []), traffic)
    rx, tx = traffic.get(interface, (0, 0)) if interface else (0, 0)

    read_sectors = written_sectors = 0
    for line in sections.get("diskstats", []):
        fields = line.split()
        if len(fields) >= 10 and _WHOLE_DISK.fullmatch(fields[2]):
            read_sectors += int(fields[5])
            written_sectors += int(fields[9])

    return HostSample(
        counters=HostCounters(
            uptime_seconds=uptime,
            cpu_total=cpu_total,
            cpu_idle=cpu_idle,
            net_rx_bytes=rx,
            net_tx_bytes=tx,
            disk_read_bytes=read_sectors * SECTOR_BYTES,
            disk_written_bytes=written_sectors * SECTOR_BYTES,
        ),
        load1=load1,
        mem_total_bytes=mem_total,
        mem_used_bytes=mem_used,
        disk_total_bytes=disk_total,
        disk_used_bytes=disk_used,
        net_interface=interface,
    )


def compute_rates(previous: HostCounters, current: HostCounters) -> HostRates | None:
    """Скорости между двумя замерами. None — если сравнивать нельзя: сервер перезагрузился (аптайм меньше)
    или счётчик сбросился (уменьшился) — иначе на графике был бы гигантский отрицательный выброс."""
    elapsed = current.uptime_seconds - previous.uptime_seconds
    deltas = (
        current.cpu_total - previous.cpu_total,
        current.cpu_idle - previous.cpu_idle,
        current.net_rx_bytes - previous.net_rx_bytes,
        current.net_tx_bytes - previous.net_tx_bytes,
        current.disk_read_bytes - previous.disk_read_bytes,
        current.disk_written_bytes - previous.disk_written_bytes,
    )
    if elapsed <= 0 or any(delta < 0 for delta in deltas):
        return None
    cpu_delta, idle_delta, rx, tx, read, written = deltas
    busy = (cpu_delta - idle_delta) / cpu_delta * 100 if cpu_delta else 0.0
    return HostRates(
        cpu_percent=round(min(max(busy, 0.0), 100.0), 2),
        net_rx_bytes_per_s=rx / elapsed,
        net_tx_bytes_per_s=tx / elapsed,
        disk_read_bytes_per_s=read / elapsed,
        disk_write_bytes_per_s=written / elapsed,
    )


async def read_host_sample(ssh_client: SSHClient) -> HostSample:
    """Один замер с сервера. Вывод команды не логируется (политика SSHClient)."""
    result = await ssh_client.execute(HOST_METRICS_COMMAND)
    if result.exit_status != 0 and "@uptime" not in result.stdout:
        raise ValueError("Host metrics command failed")
    return parse_host_metrics(result.stdout)
