"""Общее для тестов geoip (этап 24.4): синтетические файлы RIPE и списки CIDR нужного размера."""

from ipaddress import IPv4Network


def synthetic_networks(count: int, *, first: int = 20) -> list[IPv4Network]:
    """`count` несмежных глобальных /24: a.b.(2c).0 - соседние блоки не склеиваются при сжатии."""
    networks = []
    for index in range(count):
        a = first + index // 20000
        b = (index // 100) % 200
        c = (index % 100) * 2
        networks.append(IPv4Network(f"{a}.{b}.{c}.0/24"))
    return networks


def ripe_line(network: IPv4Network, *, country: str = "RU", status: str = "allocated", date: str = "20240101") -> str:
    return f"ripencc|{country}|ipv4|{network.network_address}|{network.num_addresses}|{date}|{status}"


def ripe_file(networks: list[IPv4Network], *, extra: list[str] | None = None, header_serial: str = "20261020") -> str:
    """Файл в формате delegated-ripencc-extended: заголовок, сводки, строки RU и чужие строки."""
    lines = [
        f"2|ripencc|{header_serial}|{len(networks) + 5}|19830705|{header_serial}|+0100",
        "ripencc|*|ipv4|*|1234|summary",
        "ripencc|*|asn|*|10|summary",
        "ripencc|DE|ipv4|5.9.0.0|65536|20120101|allocated",
        "ripencc|RU|ipv6|2a00:1450::|32|20120101|allocated",
        "ripencc|RU|asn|8359|1|19940101|allocated",
    ]
    lines += [ripe_line(network) for network in networks]
    lines += extra or []
    return "\n".join(lines) + "\n"


def cidr_file(networks: list[IPv4Network]) -> str:
    return "".join(f"{network}\n" for network in networks)
