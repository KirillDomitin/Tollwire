#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Конфиг клиента в формате приложения AmneziaVPN — строка vpn://.

Формат (повторяет рабочий образец, экспортированный самим AmneziaVPN):
    "vpn://" + base64url без «=» ( 4 байта длины big-endian + zlib(JSON) )   — это Qt qCompress
JSON: контейнер amnezia-awg2 с параметрами обфускации и вложенной строкой last_config (ключи, адреса, текст конфига).
Имя сервера в приложении берётся из поля description — без него AmneziaVPN показывает «СЕРВЕР 1».
"""

import base64
import ipaddress
import json
import struct
import zlib

from infrastructure.vpn.awg_config import client_config_sections, obfuscation_params, public_key_from_private
from infrastructure.vpn.config import TUNNEL_SUBNET

APP_CONTAINER = "amnezia-awg2"
APP_PROTOCOL_VERSION = "3.1"
APP_FORMAT_VERSION = 1
CLIENT_MTU = "1280"
# DNS в тексте конфига приложение подставляет само из dns1/dns2.
DNS_PLACEHOLDER_LINE = "DNS = $PRIMARY_DNS, $SECONDARY_DNS"
# Серия QR — как у экспорта в самом AmneziaVPN (client/core/utils/qrCodeUtils): метка и размер куска.
QR_MAGIC = 1984
QR_CHUNK_SIZE = 850


def _endpoint(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host.strip("[]"), int(port)


def _with_dns_placeholders(client_config: str) -> str:
    lines = [
        DNS_PLACEHOLDER_LINE if line.strip().split("=", 1)[0].strip() == "DNS" else line
        for line in client_config.splitlines()
    ]
    return "\n".join(lines) + "\n"


def amnezia_share_link(
    client_config: str, description: str, *, subnet: str = TUNNEL_SUBNET, dns: str | None = None
) -> str:
    """Строка vpn:// для импорта в AmneziaVPN. ValueError — если конфиг неполный.

    subnet — подсеть туннеля сервера (поле subnet_address), dns — «основной, запасной» для dns1/dns2; без dns берётся
    строка DNS из самого конфига клиента.
    """
    return "vpn://" + _b64url(_app_payload(client_config, description, subnet, dns))


def amnezia_qr_codes(
    client_config: str, description: str, *, subnet: str = TUNNEL_SUBNET, dns: str | None = None
) -> list[str]:
    """Тот же конфиг серией QR-кодов — в формате, который печатает экспорт самого AmneziaVPN (qrCodeUtils).

    Один код со всей строкой vpn:// получается слишком плотным (~130×130 модулей) — камера его не читает.
    Приложение режет сжатые данные на куски по 850 байт; кусок — QDataStream (big-endian):
        qint16 1984 (метка) | quint8 число кусков | quint8 номер куска | QByteArray (quint32 длина + байты)
    и в base64url без «=». Сканер AmneziaVPN собирает куски сам, в любом порядке.
    """
    payload = _app_payload(client_config, description, subnet, dns)
    parts = [payload[start : start + QR_CHUNK_SIZE] for start in range(0, len(payload), QR_CHUNK_SIZE)]
    if len(parts) > 255:  # число и номер куска — по одному байту
        raise ValueError("Config is too large for a QR series")
    return [
        _b64url(struct.pack(">hBBI", QR_MAGIC, len(parts), index, len(part)) + part) for index, part in enumerate(parts)
    ]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _app_payload(client_config: str, description: str, subnet: str, dns_override: str | None) -> bytes:
    """Документ AmneziaVPN, сжатый как Qt qCompress: 4 байта длины (big-endian) + zlib."""
    interface, peer = client_config_sections(client_config)
    try:
        address = ipaddress.ip_interface(interface["Address"].split(",")[0].strip())
        private_key = interface["PrivateKey"]
        server_public_key = peer["PublicKey"]
        psk = peer["PresharedKey"]
        host, port = _endpoint(peer["Endpoint"])
    except (KeyError, ValueError):
        # Без цепочки исключений: в их тексте могли бы оказаться куски конфига.
        raise ValueError("Client config is malformed") from None

    client_public_key = public_key_from_private(private_key)
    params = obfuscation_params(interface)
    dns_line = dns_override if dns_override is not None else interface.get("DNS", "")
    dns = [server.strip() for server in dns_line.split(",") if server.strip()]

    last_config = {
        **params,
        "allowed_ips": [ip.strip() for ip in peer.get("AllowedIPs", "0.0.0.0/0, ::/0").split(",") if ip.strip()],
        "clientId": client_public_key,
        "client_ip": str(address.ip),
        "client_priv_key": private_key,
        "client_pub_key": client_public_key,
        "config": _with_dns_placeholders(client_config),
        "hostName": host,
        "mtu": CLIENT_MTU,
        "persistent_keep_alive": peer.get("PersistentKeepalive", "25"),
        "port": port,
        "psk_key": psk,
        "server_pub_key": server_public_key,
    }
    container = {
        **params,
        "last_config": json.dumps(last_config, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        "port": str(port),
        "protocol_version": APP_PROTOCOL_VERSION,
        "subnet_address": str(ipaddress.ip_network(subnet).network_address),
        "transport_proto": "udp",
    }
    document = {
        "containers": [{"awg": container, "container": APP_CONTAINER}],
        "defaultContainer": APP_CONTAINER,
        "description": description,
        "dns1": dns[0] if dns else "1.1.1.1",
        "dns2": dns[1] if len(dns) > 1 else (dns[0] if dns else "1.0.0.1"),
        "format_version": APP_FORMAT_VERSION,
        "hostName": host,
    }
    data = json.dumps(document, ensure_ascii=False, indent=4, sort_keys=True).encode()
    return struct.pack(">I", len(data)) + zlib.compress(data, 9)
