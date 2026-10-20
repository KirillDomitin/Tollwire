#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Единый источник правды о том, какие сети можно отправлять «напрямую» (этап 24.4).

Правило 100 шлюза стоит раньше kill-switch (9000): всё, что попало в таблицу `ru`, уходит с адреса шлюза наружу через
eth0. Если туда попадёт сеть, пересекающаяся с частным / служебным пространством (docker-сеть, хост шлюза, метаданные
облака 169.254.169.254, CGNAT), клиенты VPN получат доступ к хосту. Поэтому и разбор списка (`modules/geoip/parser.py`),
и запись `ru.prefixes` на шлюзе (`gateway_config.render_ru_prefixes`) опираются на этот модуль и ни на что другое.

Модуль чистый (только `ipaddress`), поэтому его можно импортировать из любого слоя.
"""

import ipaddress
from collections.abc import Iterable
from ipaddress import IPv4Network

# Диапазоны, с которыми не должна пересекаться ни одна «прямая» сеть (пересечение, а не вхождение: 172.0.0.0/8 крупнее
# 172.16.0.0/12 и тоже накрывает его).
RESERVED_NETWORKS: tuple[IPv4Network, ...] = tuple(
    IPv4Network(text)
    for text in (
        "0.0.0.0/8",  # «эта сеть»
        "10.0.0.0/8",  # частная
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, метаданные облака
        "172.16.0.0/12",  # частная (в том числе сети Docker)
        "192.0.0.0/24",  # IETF
        "192.0.2.0/24",  # документация
        "192.88.99.0/24",  # 6to4
        "192.168.0.0/16",  # частная
        "198.18.0.0/15",  # стенды
        "198.51.100.0/24",  # документация
        "203.0.113.0/24",  # документация
        "224.0.0.0/3",  # multicast и зарезервированное до 255.255.255.255
    )
)

# Самая крупная сеть, которую шлюз вообще согласится положить в `ru.prefixes` (после слияния соседних блоков список
# может дать /11 или /10); входные правила строже.
MIN_ROUTE_PREFIXLEN = 8
# Список RIPE: реальные российские блоки не крупнее /13, всё, что больше /12, - ошибка источника или подмена.
MIN_LIST_PREFIXLEN = 12
# Ручное исключение DIRECT: не крупнее /16 (крупное исключение само по себе открывает маршруты к чужим сетям).
MIN_DIRECT_PREFIXLEN = 16


def overlaps_reserved(network: IPv4Network) -> bool:
    return any(network.overlaps(reserved) for reserved in RESERVED_NETWORKS)


def is_routable(network: object, min_prefixlen: int = MIN_ROUTE_PREFIXLEN) -> bool:
    """Годится для маршрута «напрямую»: IPv4Network не крупнее `min_prefixlen` и вне зарезервированных диапазонов."""
    return isinstance(network, IPv4Network) and network.prefixlen >= min_prefixlen and not overlaps_reserved(network)


def subtract(networks: Iterable[IPv4Network], cuts: Iterable[IPv4Network]) -> list[IPv4Network]:
    """`networks` без адресов из `cuts`, сжатый, по возрастанию. Вычитаемая сеть внутри крупной разбивает её на
    оставшиеся куски (`address_exclude`), вычитаемая крупнее - убирает целиком."""
    merged: list[IPv4Network] = list(ipaddress.collapse_addresses(networks))
    for cut in sorted(set(cuts)):
        remaining: list[IPv4Network] = []
        for network in merged:
            if not network.overlaps(cut):
                remaining.append(network)
            elif cut.prefixlen > network.prefixlen:  # cut строго внутри network
                remaining.extend(network.address_exclude(cut))
            # иначе network целиком внутри cut (или совпадает) - выбрасываем
        merged = list(ipaddress.collapse_addresses(remaining))
    return sorted(merged)


def subtract_reserved(networks: Iterable[IPv4Network]) -> list[IPv4Network]:
    return subtract(networks, RESERVED_NETWORKS)
