#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Список российских IPv4-префиксов (этап 24.4): разбор RIPE и CIDR-списков, проверки, эффективный список.

Чистые функции без сети, БД и SSH. Список приходит снаружи (RIPE или загрузка админа), поэтому разбор строгий:
мусорные строки пропускаются, но список принимается, только если валидных строк и префиксов достаточно, а префиксы
не выходят за разумные рамки; адреса не из глобального интернета (частные, loopback, multicast...) отбрасываются -
иначе подменённый файл направил бы клиентов в сеть шлюза или хоста.
"""

import hashlib
import ipaddress
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network
from typing import Literal, NamedTuple

from infrastructure.vpn.split_routes import (
    MIN_DIRECT_PREFIXLEN,
    MIN_LIST_PREFIXLEN,
    MIN_ROUTE_PREFIXLEN,
    is_routable,
    subtract,
    subtract_reserved,
)

MIN_RECORDS = 1000  # валидных строк RU/ipv4 (или CIDR) в файле
MIN_PREFIXES = 3000  # префиксов после сжатия
MAX_PREFIXES = 30000
CHANGE_LIMIT = 0.20  # изменение числа префиксов или покрытых адресов, после которого нужен админ
_RIPE_STATUSES = frozenset({"allocated", "assigned"})
_DATE_RE = re.compile(r"\d{8}")
_CIDR_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}")


class GeoipError(ValueError):
    """Список не принят: слишком мало данных, выход за рамки, не тот формат. Сообщение без содержимого файла."""


@dataclass(frozen=True)
class ParsedList:
    prefixes: list[IPv4Network]  # сжатый список, по возрастанию адреса
    version: str  # YYYYMMDD
    records: int  # валидных строк-источников
    skipped: int  # строк, отброшенных как мусор
    rejected: int = 0  # сетей, отброшенных как негодные: пересекают зарезервированные диапазоны или крупнее /12


def is_acceptable(network: IPv4Network, min_prefixlen: int = MIN_LIST_PREFIXLEN) -> bool:
    """Префикс годится для маршрута «напрямую»: не крупнее `min_prefixlen` (в списке RIPE - /12) и не пересекается с
    частными / служебными диапазонами (`infrastructure/vpn/split_routes.py`, общий источник правды со шлюзом)."""
    return is_routable(network, min_prefixlen)


class _Line(NamedTuple):
    kind: Literal["ok", "ignore", "junk"]  # ignore - чужая строка RIPE (не мусор), junk - испорченная строка списка
    networks: list[IPv4Network] = []
    date: str | None = None


_IGNORE = _Line("ignore")
_JUNK = _Line("junk")


def _ripe_line(fields: list[str]) -> _Line:
    """`ripencc|RU|ipv4|start|count|date|status`. Другая страна, IPv6, заголовок, иной статус - ignore."""
    if len(fields) < 7 or fields[1] != "RU" or fields[2] != "ipv4" or fields[6].strip().lower() not in _RIPE_STATUSES:
        return _IGNORE
    try:
        start = IPv4Address(fields[3])
        count = int(fields[4])
        end = IPv4Address(int(start) + count - 1)
    except ValueError:  # в том числе адрес за границей пространства при слишком большом count
        return _JUNK
    if count < 1:
        return _JUNK
    date = fields[5] if _DATE_RE.fullmatch(fields[5]) else None
    return _Line("ok", list(ipaddress.summarize_address_range(start, end)), date)


def _line(line: str) -> _Line:
    if "|" in line:
        return _ripe_line([part.strip() for part in line.split("|")])
    if _CIDR_RE.fullmatch(line):
        try:
            return _Line("ok", [ipaddress.IPv4Network(line, strict=True)])
        except ValueError:
            return _JUNK
    return _JUNK


def parse_list(
    text: str,
    *,
    today: datetime | None = None,
    min_records: int = MIN_RECORDS,
    min_prefixes: int = MIN_PREFIXES,
    max_prefixes: int = MAX_PREFIXES,
    strict: bool = False,
) -> ParsedList:
    """Разбирает файл RIPE (`delegated-ripencc-extended`) или список CIDR по строке (`#` - комментарий).

    Берутся строки RU / ipv4 / allocated|assigned; count не обязан быть степенью двойки (диапазон раскладывается
    в CIDR), IPv6 и прочие страны игнорируются. Мусорные строки пропускаются; если валидных строк меньше min_records
    или префиксов после сжатия вне [min_prefixes, max_prefixes] - GeoipError. Сеть, пересекающая зарезервированные
    диапазоны или крупнее /12, отбрасывается и считается в `rejected` (источник RIPE не должен валиться из-за одной
    строки); `strict=True` (загрузка админа) вместо этого отклоняет весь список.
    """
    networks: list[IPv4Network] = []
    records = skipped = rejected = 0
    dates: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        found = _line(line)
        if found.kind == "ignore":
            continue
        if found.kind == "junk":
            skipped += 1
            continue
        good = [network for network in found.networks if is_acceptable(network)]
        rejected += len(found.networks) - len(good)
        if not good:
            continue
        records += 1
        networks.extend(good)
        if found.date:
            dates.append(found.date)
    if strict and rejected:
        raise GeoipError(
            "The list contains a network that cannot be routed directly (private, reserved or larger than /12)"
        )
    if records < min_records:
        raise GeoipError(f"Too few valid records in the list: {records}, at least {min_records} expected")
    prefixes = sorted(ipaddress.collapse_addresses(networks))
    if not min_prefixes <= len(prefixes) <= max_prefixes:
        raise GeoipError(
            f"The list has {len(prefixes)} prefixes after merging, expected from {min_prefixes} to {max_prefixes}"
        )
    version = max(dates) if dates else (today or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return ParsedList(prefixes=prefixes, version=version, records=records, skipped=skipped, rejected=rejected)


def addresses_count(prefixes: Iterable[IPv4Network]) -> int:
    return sum(network.num_addresses for network in prefixes)


def render_list(prefixes: Iterable[IPv4Network]) -> str:
    """Список в виде для хранения: CIDR по строке, по возрастанию."""
    return "".join(f"{network}\n" for network in sorted(prefixes))


def parse_stored(text: str) -> list[IPv4Network]:
    """Сохранённый список (render_list) обратно. Хранится панелью, но проверяется так же строго: порча данных не
    должна дойти до шлюза."""
    result: list[IPv4Network] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        network = ipaddress.IPv4Network(line.strip(), strict=True)
        if not is_acceptable(network, MIN_ROUTE_PREFIXLEN):
            raise GeoipError("The stored list contains a prefix that cannot be routed directly")
        result.append(network)
    return result


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def needs_confirmation(
    old_prefixes: int, old_addresses: int, new_prefixes: int, new_addresses: int, limit: float = CHANGE_LIMIT
) -> bool:
    """Новый список отличается от активного больше чем на `limit` по числу префиксов или покрытых адресов."""
    if old_prefixes <= 0 or old_addresses <= 0:
        return False
    prefixes_change = abs(new_prefixes - old_prefixes) / old_prefixes
    addresses_change = abs(new_addresses - old_addresses) / old_addresses
    return prefixes_change > limit or addresses_change > limit


def _intersection_addresses(old: Sequence[IPv4Network], new: Sequence[IPv4Network]) -> int:
    """Сколько адресов есть и в старом, и в новом списке (оба - сжатые, непересекающиеся)."""
    left = sorted((int(n.network_address), int(n.broadcast_address)) for n in old)
    right = sorted((int(n.network_address), int(n.broadcast_address)) for n in new)
    total = i = j = 0
    while i < len(left) and j < len(right):
        low = max(left[i][0], right[j][0])
        high = min(left[i][1], right[j][1])
        if low <= high:
            total += high - low + 1
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return total


def requires_confirmation(old: Sequence[IPv4Network], new: Sequence[IPv4Network], limit: float = CHANGE_LIMIT) -> bool:
    """Нужно ли подтверждение админа. Кроме изменения размера (`needs_confirmation`) учитывается ПЕРЕСЕЧЕНИЕ: замена
    части сетей на чужие при тех же суммах не должна проходить сама. Нужно подтверждение, если от адресов активного
    списка в новом осталось меньше (1 - limit) или в новом больше `limit` адресов, которых раньше не было."""
    old_set = list(ipaddress.collapse_addresses(old))
    new_set = list(ipaddress.collapse_addresses(new))
    old_addresses, new_addresses = addresses_count(old_set), addresses_count(new_set)
    if needs_confirmation(len(old_set), old_addresses, len(new_set), new_addresses, limit):
        return True
    if old_addresses <= 0 or new_addresses <= 0:
        return False
    common = _intersection_addresses(old_set, new_set)
    return common / old_addresses < 1 - limit or (new_addresses - common) / new_addresses > limit


def validate_override(text: str, *, direct: bool = True) -> IPv4Network:
    """Сеть исключения админа: строгий IPv4 CIDR без хост-битов. DIRECT открывает маршрут с адреса шлюза, поэтому он не
    крупнее /16 и не пересекается с частными / служебными диапазонами. TUNNEL только вычитает из списка: годится любая
    сеть не крупнее /8 (в том числе зарезервированная - вычитать нечего, но и вреда нет)."""
    value = text.strip()
    if not _CIDR_RE.fullmatch(value):
        raise GeoipError("Network must be an IPv4 CIDR such as 5.45.200.0/24")
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except ValueError:
        raise GeoipError("Network must be an IPv4 CIDR with the host bits zero") from None
    if direct:
        if not is_acceptable(network, MIN_DIRECT_PREFIXLEN):
            raise GeoipError(
                "A direct network must be public (no private, reserved or multicast ranges) and not larger than /16"
            )
    elif network.prefixlen < MIN_ROUTE_PREFIXLEN:
        raise GeoipError("A tunnel network must not be larger than /8")
    return network


def effective_list(
    active: Sequence[IPv4Network],
    direct: Iterable[IPv4Network] = (),
    tunnel: Iterable[IPv4Network] = (),
    protected: Iterable[IPv4Network] = (),
) -> list[IPv4Network]:
    """((активный список ∪ DIRECT) − зарезервированные диапазоны − TUNNEL − protected), сжатый, по возрастанию.
    `protected` - адреса самого шлюза и его выходов (/32): через них идут туннели и управление, прямым маршрутом их
    закрывать нельзя. Зарезервированные диапазоны вычитаются всегда, даже если в хранилище попал лишний префикс."""
    return subtract(subtract_reserved([*active, *direct]), [*tunnel, *protected])
