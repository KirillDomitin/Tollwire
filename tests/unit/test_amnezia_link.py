import base64
import json
import struct
import zlib
from ipaddress import IPv4Address
from typing import Any

import pytest

from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.amnezia_link import amnezia_qr_codes, amnezia_share_link
from infrastructure.vpn.awg_config import (
    generate_secret_keys,
    interface_values,
    render_awg_config,
    render_client_config,
)
from infrastructure.vpn.provider import VPNProvider


def decode(link: str) -> dict[str, Any]:
    """Обратная операция к сборке: так же, как это делает приложение AmneziaVPN."""
    assert link.startswith("vpn://")
    body = link.removeprefix("vpn://")
    assert "=" not in body  # base64url без выравнивания, как в образце из приложения
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    (size,) = struct.unpack(">I", raw[:4])
    data = zlib.decompress(raw[4:])
    assert size == len(data)  # заголовок qCompress — длина распакованных данных
    document: dict[str, Any] = json.loads(data)
    return document


@pytest.fixture
def server_config() -> str:
    return render_awg_config(generate_secret_keys().private_key, 31000)


def client_config(server_config: str, endpoint_host: str = "203.0.113.10") -> tuple[str, Any, str]:
    client = generate_secret_keys()
    server_public = generate_secret_keys().public_key
    text = render_client_config(
        server_config=server_config,
        client_private_key=client.private_key,
        client_address=IPv4Address("10.8.1.5"),
        psk=client.psk,
        server_public_key=server_public,
        endpoint_host=endpoint_host,
    )
    return text, client, server_public


def test_link_carries_server_name_and_matches_app_layout(server_config: str) -> None:
    text, _, _ = client_config(server_config)
    document = decode(amnezia_share_link(text, "🇺🇸 US2"))

    assert document["description"] == "🇺🇸 US2"  # имя сервера в приложении — вместо «СЕРВЕР 1»
    assert document["defaultContainer"] == "amnezia-awg2"
    assert document["hostName"] == "203.0.113.10"
    assert (document["dns1"], document["dns2"]) == ("1.1.1.1", "1.0.0.1")
    (container,) = document["containers"]
    assert container["container"] == "amnezia-awg2"
    awg = container["awg"]
    assert awg["port"] == "31000" and awg["transport_proto"] == "udp"
    assert awg["subnet_address"] == "10.8.1.0"
    # Параметры обфускации — те же, что в конфиге клиента (а значит, и на сервере).
    client_values = interface_values(text)
    for key in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H4"):
        assert awg[key] == client_values[key]


def test_last_config_holds_keys_and_config_with_dns_placeholders(server_config: str) -> None:
    text, client, server_public = client_config(server_config)
    awg = decode(amnezia_share_link(text, "US2"))["containers"][0]["awg"]
    last = json.loads(awg["last_config"])

    assert last["client_priv_key"] == client.private_key
    assert last["client_pub_key"] == last["clientId"] == client.public_key
    assert last["server_pub_key"] == server_public
    assert last["psk_key"] == client.psk
    assert last["client_ip"] == "10.8.1.5"
    assert last["port"] == 31000 and last["hostName"] == "203.0.113.10"
    assert last["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    # DNS подставляет приложение из dns1/dns2 — в тексте только заглушки.
    assert "DNS = $PRIMARY_DNS, $SECONDARY_DNS" in last["config"]
    assert "DNS = 1.1.1.1" not in last["config"]
    assert f"PrivateKey = {client.private_key}" in last["config"]


def test_ipv6_endpoint_host_is_unwrapped(server_config: str) -> None:
    text, _, _ = client_config(server_config, endpoint_host="2001:db8::1")
    document = decode(amnezia_share_link(text, "v6"))
    assert document["hostName"] == "2001:db8::1"
    assert document["containers"][0]["awg"]["port"] == "31000"


def test_malformed_config_is_rejected_without_leaking_it() -> None:
    with pytest.raises(ValueError, match="malformed") as error:
        amnezia_share_link("[Interface]\nPrivateKey = secret-ish\n", "US2")
    assert "secret-ish" not in str(error.value)


def read_qr_chunk(text: str) -> tuple[int, int, int, bytes]:
    """Разбор куска так же, как его читает сканер AmneziaVPN (ImportController::parseQrCodeChunk)."""
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    magic, count, index, size = struct.unpack(">hBBI", raw[:8])
    part = raw[8:]
    assert len(part) == size
    return magic, count, index, part


def test_qr_series_reassembles_into_the_same_app_document(server_config: str) -> None:
    text, _, _ = client_config(server_config)
    codes = amnezia_qr_codes(text, "US2")
    link_body = amnezia_share_link(text, "US2").removeprefix("vpn://")
    payload = base64.urlsafe_b64decode(link_body + "=" * (-len(link_body) % 4))

    chunks = [read_qr_chunk(code) for code in reversed(codes)]  # сканер собирает в любом порядке
    assert len(codes) >= 2  # одним кодом весь конфиг не помещается в читаемый QR
    assert {magic for magic, *_ in chunks} == {1984}
    assert {count for _, count, _, _ in chunks} == {len(codes)}
    assert all(len(part) <= 850 for *_, part in chunks)
    joined = b"".join(part for *_, part in sorted(chunks, key=lambda chunk: chunk[2]))
    assert joined == payload
    assert decode("vpn://" + base64.urlsafe_b64encode(joined).rstrip(b"=").decode())["description"] == "US2"
    assert all("=" not in code for code in codes)


def test_only_providers_with_an_app_format_give_a_link(server_config: str) -> None:
    text, _, _ = client_config(server_config)
    assert VPNProvider.share_link(text, "US2") is None
    assert VPNProvider.share_qr_codes(text, "US2") is None
    link = AmneziaWGProvider.share_link(text, "US2")
    assert link is not None and decode(link)["description"] == "US2"
    assert AmneziaWGProvider.share_qr_codes(text, "US2")
