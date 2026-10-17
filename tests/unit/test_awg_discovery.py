"""Разбор чужого конфига AmneziaWG для поиска (этап 23.2): clientsTable приложения AmneziaVPN и [Peer] без
комментариев `# client:`. Чистые функции, без SSH. Новые имена берутся внутри теста — нет функции, падает тест,
а не вся коллекция."""

import dataclasses
import json
import unicodedata
from typing import Any

import pytest

from infrastructure.vpn import awg_config
from infrastructure.vpn.awg_config import generate_secret_keys
from tests.fakes.fake_ssh import AppPeer, FakeServer, clients_table_json

ONE_MIB = 1024 * 1024


def parse_clients_table(text: str) -> dict[str, str]:
    return awg_config.parse_clients_table(text)  # type: ignore[attr-defined, no-any-return]


def discover_peers(config: str) -> list[Any]:
    return awg_config.discover_peers(config)  # type: ignore[attr-defined, no-any-return]


def key() -> str:
    return generate_secret_keys().public_key


# --- parse_clients_table ---


def test_clients_table_array_format_maps_public_key_to_name() -> None:
    a, b = key(), key()
    assert parse_clients_table(clients_table_json({a: "Phone", b: "Laptop"})) == {a: "Phone", b: "Laptop"}


def test_clients_table_legacy_object_format_with_client_name() -> None:
    a, b = key(), key()
    assert parse_clients_table(clients_table_json({a: "Phone", b: "Laptop"}, legacy=True)) == {a: "Phone", b: "Laptop"}


def test_clients_table_object_format_with_name_in_user_data() -> None:
    a = key()
    assert parse_clients_table(json.dumps({a: {"userData": {"clientName": "Tablet"}}})) == {a: "Tablet"}


def test_clients_table_empty_array_and_object_give_nothing() -> None:
    assert parse_clients_table("[]") == {} and parse_clients_table("{}") == {}


@pytest.mark.parametrize(
    "text",
    ["", "not json at all", "{", "[{", '[{"clientId": ', "null", "123", '"just a string"', "true", "\x00\x01\x02"],
    ids=repr,
)
def test_clients_table_garbage_gives_empty_dict(text: str) -> None:
    assert parse_clients_table(text) == {}


def test_clients_table_deeply_nested_json_does_not_raise() -> None:
    """json.loads на глубокой вложенности бросает RecursionError — наружу он вылетать не должен."""
    assert parse_clients_table("[" * 200_000) == {}
    assert parse_clients_table('{"a":' * 50_000) == {}


def test_clients_table_huge_number_does_not_raise() -> None:
    assert parse_clients_table("1" * 5000) == {}  # int() от такой строки в Python падает ValueError


def test_clients_table_input_over_one_mib_is_ignored_even_if_valid() -> None:
    valid = clients_table_json({key(): "Phone"})
    assert parse_clients_table(valid + " " * (ONE_MIB - len(valid) + 1)) == {}  # на байт больше предела


def test_clients_table_input_of_exactly_one_mib_is_still_parsed() -> None:
    a = key()
    valid = clients_table_json({a: "Phone"})
    assert parse_clients_table(valid + " " * (ONE_MIB - len(valid))) == {a: "Phone"}


def test_clients_table_skips_entries_without_key_or_string_name() -> None:
    good = key()
    table = [
        {"clientId": good, "userData": {"clientName": "Good"}},
        {"userData": {"clientName": "no key"}},
        {"clientId": 12345, "userData": {"clientName": "numeric key"}},
        {"clientId": key(), "userData": {"clientName": 42}},
        {"clientId": key(), "userData": {"clientName": None}},
        {"clientId": key(), "userData": {}},
        {"clientId": key()},
        {"clientId": key(), "userData": "not a dict"},
        "string entry",
        None,
        7,
        [],
    ]
    assert parse_clients_table(json.dumps(table)) == {good: "Good"}


def test_clients_table_object_format_skips_bad_values() -> None:
    good = key()
    table = {good: {"clientName": "Good"}, key(): "not a dict", key(): {"clientName": 1}, key(): None, key(): {}}
    assert parse_clients_table(json.dumps(table)) == {good: "Good"}


def test_clients_table_name_is_cut_to_100_characters() -> None:
    a = key()
    assert parse_clients_table(clients_table_json({a: "n" * 150})) == {a: "n" * 100}


def test_clients_table_name_of_exactly_100_characters_is_kept() -> None:
    a = key()
    assert parse_clients_table(clients_table_json({a: "n" * 100})) == {a: "n" * 100}


@pytest.mark.parametrize("bad", ["\n", "\r\n", "\t", "\x00", "\x1b", "\x7f", "\u0085", " "], ids=repr)
def test_clients_table_name_has_no_control_characters(bad: str) -> None:
    """Имя попадает в журнал, ответ API и интерфейс: перевод строки позволил бы подделать строку журнала."""
    a = key()
    (name,) = parse_clients_table(clients_table_json({a: f"evil{bad}name"})).values()
    assert "evil" in name and "name" in name
    assert not any(unicodedata.category(char) == "Cc" for char in name)


def test_clients_table_cleaned_name_still_fits_in_100_characters() -> None:
    a = key()
    (name,) = parse_clients_table(clients_table_json({a: ("x\n" * 120)})).values()
    assert len(name) <= 100 and "\n" not in name


def test_clients_table_keeps_unicode_names() -> None:
    a = key()
    assert parse_clients_table(clients_table_json({a: "Телефон Маши 📱"})) == {a: "Телефон Маши 📱"}


def test_clients_table_ignores_extra_fields_of_entries() -> None:
    a = key()
    entry = {"clientId": a, "userData": {"clientName": "Phone", "creationDate": "x", "dataReceived": "1 KiB"}, "x": 1}
    assert parse_clients_table(json.dumps([entry])) == {a: "Phone"}


# --- discover_peers ---


def config_with(*peers: str) -> str:
    interface = "[Interface]\nPrivateKey = SERVER-PRIVATE\nAddress = 10.8.1.0/24\nListenPort = 34567\n"
    return interface + "".join(f"\n[Peer]\n{peer}\n" for peer in peers)


def peer(public_key: str, *lines: str) -> str:
    return "\n".join([f"PublicKey = {public_key}", f"PresharedKey = SECRET-PSK-{public_key[:5]}", *lines])


def test_discover_peers_reads_key_and_ipv4_address_without_mask() -> None:
    a = key()
    (found,) = discover_peers(config_with(peer(a, "AllowedIPs = 10.8.1.2/32")))
    assert (found.public_key, found.address, found.supported) == (a, "10.8.1.2", True)


def test_discover_peers_without_peers_is_empty() -> None:
    assert discover_peers(config_with()) == []


def test_discover_peers_keeps_order_of_the_file() -> None:
    a, b, c = key(), key(), key()
    config = config_with(
        peer(a, "AllowedIPs = 10.8.1.2/32"), peer(b, "AllowedIPs = 10.8.1.3/32"), peer(c, "AllowedIPs = 10.8.1.4/32")
    )
    assert [p.public_key for p in discover_peers(config)] == [a, b, c]


def test_discover_peers_peer_without_allowed_ips_is_unsupported() -> None:
    a = key()
    (found,) = discover_peers(config_with(peer(a)))
    assert found.public_key == a and found.supported is False and found.address is None


def test_discover_peers_several_allowed_ips_entries_are_unsupported() -> None:
    a = key()
    (found,) = discover_peers(config_with(peer(a, "AllowedIPs = 10.8.1.2/32, 10.9.0.0/32")))
    assert found.supported is False


@pytest.mark.parametrize("allowed", ["fd00::2/128", "10.8.1.2/32, fd00::2/128", "::/0", "fd00::/32", "::1/32"])
def test_discover_peers_ipv6_is_unsupported(allowed: str) -> None:
    (found,) = discover_peers(config_with(peer(key(), f"AllowedIPs = {allowed}")))
    assert found.supported is False


@pytest.mark.parametrize("allowed", ["10.8.1.0/24", "0.0.0.0/0", "10.8.1.2/31", "10.8.1.2/16"])
def test_discover_peers_mask_other_than_32_is_unsupported(allowed: str) -> None:
    (found,) = discover_peers(config_with(peer(key(), f"AllowedIPs = {allowed}")))
    assert found.supported is False


@pytest.mark.parametrize(
    "allowed", ["garbage", "999.1.1.1/32", "10.8.1.2/99", "10.8.1.2/", "/32", "10.8.1.2/32;rm -rf /"]
)
def test_discover_peers_garbage_in_allowed_ips_is_unsupported_not_an_error(allowed: str) -> None:
    (found,) = discover_peers(config_with(peer(key(), f"AllowedIPs = {allowed}")))
    assert found.supported is False


def test_discover_peers_one_bad_peer_does_not_hide_the_others() -> None:
    good, bad = key(), key()
    found = discover_peers(config_with(peer(bad, "AllowedIPs = ::/0"), peer(good, "AllowedIPs = 10.8.1.5/32")))
    assert {p.public_key: p.supported for p in found} == {bad: False, good: True}


def test_discover_peers_skips_peer_without_public_key() -> None:
    a = key()
    config = config_with("AllowedIPs = 10.8.1.9/32", peer(a, "AllowedIPs = 10.8.1.2/32"))
    assert [p.public_key for p in discover_peers(config)] == [a]


def test_discover_peers_does_not_take_names_from_comments() -> None:
    """У приложения AmneziaVPN комментариев `# client:` нет; результат — только ключ, адрес и флаг."""
    a = key()
    (found,) = discover_peers(config_with(f"# client: Phone\n{peer(a, 'AllowedIPs = 10.8.1.2/32')}"))
    assert {f.name for f in dataclasses.fields(found)} == {"public_key", "address", "supported"}


def test_discovered_peer_does_not_carry_secrets() -> None:
    a = key()
    found = discover_peers(config_with(peer(a, "AllowedIPs = 10.8.1.2/32")))
    assert "SERVER-PRIVATE" not in repr(found) and "SECRET-PSK" not in repr(found)


def test_discover_peers_ignores_interface_section_keys() -> None:
    """PublicKey/AllowedIPs в [Interface] — не peer."""
    config = "[Interface]\nPublicKey = " + key() + "\nAllowedIPs = 10.8.1.2/32\nListenPort = 1\n"
    assert discover_peers(config) == []


def test_discover_peers_works_on_a_config_rendered_by_the_fake_app() -> None:
    a, b = key(), key()
    app = FakeServer().add_amnezia_app(
        "amnezia-awg2", peers=[AppPeer(a, "10.8.1.2/32"), AppPeer(b, "10.8.1.3/32, 10.8.1.4/32")]
    )
    assert [(p.public_key, p.supported) for p in discover_peers(app.render_config())] == [(a, True), (b, False)]


# --- ввод с сервера не должен портить JSON отчёта и БД (jsonb не принимает \u0000 и одиночные суррогаты) ---


@pytest.mark.parametrize("bad", ["\ud800", "\udfff", "‮", " ", " ", "​"], ids=lambda c: hex(ord(c)))
def test_clients_table_name_drops_surrogates_and_invisible_separators(bad: str) -> None:
    a = key()
    (name,) = parse_clients_table(json.dumps({a: {"clientName": f"x{bad}y"}})).values()
    assert name == "xy"
    json.dumps(name).encode("utf-8")  # без суррогатов строка кодируется в UTF-8


@pytest.mark.parametrize(
    "bad_key", ["", "short", "A" * 44, "A" * 43 + "\x00", "\x00" + "A" * 42 + "=", "A" * 42 + "=\n"]
)
def test_discover_peers_skips_keys_that_are_not_wireguard_keys(bad_key: str) -> None:
    config = config_with(peer(bad_key, "AllowedIPs = 10.8.1.2/32"), peer(key(), "AllowedIPs = 10.8.1.3/32"))
    peers = awg_config.discover_peers(config)
    assert [p.address for p in peers] == ["10.8.1.3"]


@pytest.mark.parametrize("stamp", ["²", "99999999999999999", "-1", "1.5", "１２３", "9" * 40])
def test_latest_handshakes_ignores_garbage_lines_without_failing(stamp: str) -> None:
    good = key()
    output = f"{key()}\t{stamp}\n{good}\t1700000000\n"
    assert set(awg_config.parse_latest_handshakes(output)) == {good}
