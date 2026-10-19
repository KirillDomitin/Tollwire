"""Раскладка контейнера (ContainerLayout): провайдер с другой раскладкой (будущий «шлюз») и неизменность обычной.

Подкласс AmneziaWGProvider меняет только атрибут класса `layout`; все команды и пути обязаны собираться из него —
ни одной строки обычного протокола (имя контейнера, папка, интерфейс, подсеть) в них быть не должно.
"""

import base64
import json
import struct
import zlib
from dataclasses import FrozenInstanceError, replace
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, ClassVar

import pytest

from core.exceptions import AddressPoolExhaustedError
from core.ssh_exception import CommandExecutionError, ForeignContainerError
from infrastructure.vpn import amnezia
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.amnezia_link import amnezia_qr_codes, amnezia_share_link
from infrastructure.vpn.awg_config import (
    Obfuscation,
    append_peer,
    ensure_nat_hooks,
    generate_secret_keys,
    interface_values,
    list_peers,
    pick_free_address,
    render_awg_config,
    render_client_config,
    render_peer_block,
)
from infrastructure.vpn.config import (
    CLIENT_DNS,
    CONTAINER_NAME,
    CONTAINER_NAME_FILTER,
    DEFAULT_LAYOUT,
    HOST_CONFIG_DIR,
    NAT_POST_DOWN,
    NAT_POST_UP,
    NAT_RULE,
    TUNNEL_SUBNET,
    ContainerLayout,
    nat_post_down,
    nat_post_up,
    nat_rule,
)
from infrastructure.vpn.provider import ProviderState, VPNProvider
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient

GW = ContainerLayout(
    container_name="vpn-admin-gw",
    host_config_dir="/opt/vpn-admin/gw",
    container_config_dir="/etc/vpn-admin/gw",
    config_file_name="gw0.conf",
    interface_name="gw0",
    tunnel_subnet="10.77.0.0/22",
    client_dns="10.77.0.1",
    server_address="10.77.0.1/22",
)
# Строки обычного протокола: в командах и путях шлюза их быть не должно.
DEFAULT_MARKERS = ("amnezia-awg", "/opt/vpn-admin/awg", "awg0", "/etc/amnezia/awg", "10.8.1.")
GOLDEN = Path(__file__).parent / "golden" / "provider_default_commands.json"


class GatewayProvider(AmneziaWGProvider):
    layout: ClassVar[ContainerLayout] = GW


@pytest.fixture(autouse=True)
def fixed_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(amnezia, "pick_udp_port", lambda: 31337)


async def full_flow(provider_class: type[AmneziaWGProvider], fake: FakeServer) -> None:
    """install → create_client → sync → health → повторный install → revoke → uninstall."""
    async with FakeSSHClient(fake) as ssh:
        provider = provider_class(ssh)
        await provider.install()
        client = await provider.create_client("alice")
        await provider.sync()
        await provider.health_check()
        await provider.install()
        await provider.revoke_client(client.public_key)
        await provider.uninstall()


def decode(link: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(link.removeprefix("vpn://") + "=" * (-len(link.removeprefix("vpn://")) % 4))
    document: dict[str, Any] = json.loads(zlib.decompress(raw[4:]))
    return document


# --- сама раскладка --------------------------------------------------------------------------------------------


def test_default_layout_is_built_from_the_existing_constants() -> None:
    layout = DEFAULT_LAYOUT
    assert (layout.container_name, layout.host_config_dir, layout.container_config_dir) == (
        "amnezia-awg",
        "/opt/vpn-admin/awg",
        "/etc/amnezia/awg",
    )
    assert (layout.config_file_name, layout.interface_name) == ("awg0.conf", "awg0")
    assert (layout.tunnel_subnet, layout.client_dns) == ("10.8.1.0/24", "1.1.1.1, 1.0.0.1")
    assert layout.container_filter == CONTAINER_NAME_FILTER == "'name=^/?amnezia-awg$'"
    assert layout.host_config_file == "/opt/vpn-admin/awg/awg0.conf"
    assert layout.container_config_file == "/etc/amnezia/awg/awg0.conf"
    assert layout.interface_address == TUNNEL_SUBNET  # Address по умолчанию — сама подсеть (сервер на .0)
    assert (layout.nat_rule, layout.nat_post_up, layout.nat_post_down) == (NAT_RULE, NAT_POST_UP, NAT_POST_DOWN)
    assert AmneziaWGProvider.layout is DEFAULT_LAYOUT


def test_layout_is_immutable_and_nat_follows_subnet() -> None:
    with pytest.raises(FrozenInstanceError):
        DEFAULT_LAYOUT.container_name = "other"  # type: ignore[misc]
    assert GW.container_filter == "'name=^/?vpn-admin-gw$'"
    assert GW.interface_address == "10.77.0.1/22"
    assert GW.nat_rule == nat_rule("10.77.0.0/22") == "POSTROUTING -s 10.77.0.0/22 -o eth0 -j MASQUERADE"
    assert nat_post_up("10.77.0.0/22") in GW.nat_post_up and "10.8.1." not in GW.nat_post_up
    assert GW.nat_post_down == nat_post_down("10.77.0.0/22")


# --- (a) команды шлюза ----------------------------------------------------------------------------------------


async def test_gateway_commands_use_only_its_own_layout() -> None:
    fake = FakeServer(layout=GW)
    await full_flow(GatewayProvider, fake)

    assert fake.commands
    for command in fake.commands:
        for marker in DEFAULT_MARKERS:
            assert marker not in command, f"{marker!r} in {command!r}"
    for path in [*fake.reads, *fake.writes]:
        assert path == "/opt/vpn-admin/gw/gw0.conf"
    assert fake.files == {}  # uninstall убрал папку шлюза целиком

    joined = "\n".join(" ".join(command.split()) for command in fake.commands)
    assert "docker ps -a --filter 'name=^/?vpn-admin-gw$' --format '{{.State}}'" in joined
    assert "docker run -d --name vpn-admin-gw --restart unless-stopped" in joined
    assert "-p 31337:31337/udp -v /opt/vpn-admin/gw:/etc/vpn-admin/gw " in joined
    assert "sh -c 'awg-quick up /etc/vpn-admin/gw/gw0.conf && tail -f /dev/null'" in joined
    assert "docker exec vpn-admin-gw awg show gw0 public-key" in joined
    assert (
        "docker exec vpn-admin-gw sh -c 'awg-quick strip /etc/vpn-admin/gw/gw0.conf > /tmp/gw0.stripped"
        " && awg syncconf gw0 /tmp/gw0.stripped; rc=$?; rm -f /tmp/gw0.stripped; exit $rc'"
    ) in joined
    assert "docker exec vpn-admin-gw awg show gw0 listen-port" in joined
    assert "docker exec vpn-admin-gw iptables -t nat -C POSTROUTING -s 10.77.0.0/22 -o eth0 -j MASQUERADE" in joined
    assert "grep 'ListenPort' /opt/vpn-admin/gw/gw0.conf | cut -d' ' -f3" in joined
    assert "docker stop vpn-admin-gw && docker rm vpn-admin-gw" in joined
    assert "rm -rf /opt/vpn-admin/gw" in joined


async def test_gateway_server_config_client_and_health() -> None:
    fake = FakeServer(layout=GW)
    async with FakeSSHClient(fake) as ssh:
        provider = GatewayProvider(ssh)
        status = await provider.install()
        assert status.server_status == ProviderState.RUNNING and status.port == 31337
        client = await provider.create_client("alice")
        health = await provider.health_check()

    server_config = fake.files["/opt/vpn-admin/gw/gw0.conf"]
    values = interface_values(server_config)
    assert values["Address"] == "10.77.0.1/22"
    assert values["PostUp"] == GW.nat_post_up and values["PostDown"] == GW.nat_post_down
    assert [peer.address for peer in list_peers(server_config)] == ["10.77.0.2"]
    assert client.address == "10.77.0.2"
    assert "DNS = 10.77.0.1\n" in client.config and "1.1.1.1" not in client.config
    assert "Address = 10.77.0.2/32" in client.config
    assert health.installed and all(check.ok for check in health.checks), health.checks
    assert health.clients_total == 1


async def test_gateway_lives_next_to_the_default_container_without_touching_it() -> None:
    fake = FakeServer(layout=GW)
    fake.containers[CONTAINER_NAME] = "running"  # обычный протокол панели на том же сервере
    fake.mounts[CONTAINER_NAME] = [HOST_CONFIG_DIR]
    fake.files[f"{HOST_CONFIG_DIR}/awg0.conf"] = "default config"
    await full_flow(GatewayProvider, fake)

    assert fake.containers == {CONTAINER_NAME: "running"}
    assert fake.files == {f"{HOST_CONFIG_DIR}/awg0.conf": "default config"}
    assert not any(CONTAINER_NAME in command or HOST_CONFIG_DIR in command for command in fake.commands)


async def test_gateway_ignores_container_with_similar_name() -> None:
    fake = FakeServer(layout=GW)
    fake.containers["vpn-admin-gw2"] = "running"
    async with FakeSSHClient(fake) as ssh:
        status = await GatewayProvider(ssh).get_status()
    assert status.server_status == ProviderState.NOT_INSTALLED


# --- защита 23.1 для другой раскладки --------------------------------------------------------------------------


@pytest.fixture(params=["foreign_volume", "default_panel_folder"])
def foreign_gateway(request: pytest.FixtureRequest) -> FakeServer:
    """Контейнер с именем шлюза, созданный не панелью: том приложения или папка обычного протокола — не папка шлюза."""
    fake = FakeServer(layout=GW)
    source = "/var/lib/docker/volumes/vpn-admin-gw/_data" if request.param == "foreign_volume" else HOST_CONFIG_DIR
    fake.add_foreign_container(source=source)
    return fake


@pytest.mark.parametrize(
    "operation",
    [
        lambda p: p.install(),
        lambda p: p.create_client("bob"),
        lambda p: p.revoke_client(generate_secret_keys().public_key),
        lambda p: p.restore_client("bob", client_config(gateway_config())),
    ],
)
async def test_foreign_container_with_gateway_name_is_refused(foreign_gateway: FakeServer, operation: Any) -> None:
    async with FakeSSHClient(foreign_gateway) as ssh:
        with pytest.raises(ForeignContainerError, match="vpn-admin-gw"):
            await operation(GatewayProvider(ssh))
    assert foreign_gateway.mutating_commands() == [] and foreign_gateway.writes == []


async def test_foreign_container_is_not_started_stopped_or_reported(foreign_gateway: FakeServer) -> None:
    async with FakeSSHClient(foreign_gateway) as ssh:
        provider = GatewayProvider(ssh)
        assert (await provider.uninstall()).server_status == ProviderState.NOT_INSTALLED
        assert (await provider.get_status()).server_status == ProviderState.NOT_INSTALLED
        assert await provider.list_clients() == []
        health = await provider.health_check()
    assert not health.installed and "vpn-admin-gw" in (health.checks[0].detail or "")
    assert foreign_gateway.mutating_commands() == [] and foreign_gateway.containers == {"vpn-admin-gw": "running"}


async def test_default_provider_does_not_adopt_a_container_mounting_the_gateway_folder() -> None:
    """Обратное тоже верно: контейнер `amnezia-awg`, смонтировавший папку шлюза, для обычного провайдера чужой."""
    fake = FakeServer()
    fake.containers[CONTAINER_NAME] = "running"
    fake.mounts[CONTAINER_NAME] = [GW.host_config_dir]
    async with FakeSSHClient(fake) as ssh:
        with pytest.raises(ForeignContainerError):
            await AmneziaWGProvider(ssh).install()


async def test_gateway_container_that_cannot_be_inspected_is_an_error_not_a_free_name() -> None:
    fake = FakeServer(layout=GW)
    fake.ps_fails = True
    async with FakeSSHClient(fake) as ssh:
        with pytest.raises(CommandExecutionError):
            await GatewayProvider(ssh).install()
    assert fake.mutating_commands() == []


# --- (б) адреса в /22 ------------------------------------------------------------------------------------------


def gateway_config() -> str:
    return render_awg_config(
        generate_secret_keys().private_key, 31000, tunnel_subnet=GW.tunnel_subnet, address=GW.interface_address
    )


def with_peers(config: str, addresses: list[IPv4Address]) -> str:
    for address in addresses:
        config = append_peer(config, render_peer_block("c", generate_secret_keys().public_key, "psk", address))
    return config


def test_first_client_in_a_22_subnet_is_dot_two() -> None:
    config = gateway_config()
    assert interface_values(config)["Address"] == "10.77.0.1/22"
    assert pick_free_address(config) == IPv4Address("10.77.0.2")  # не адрес интерфейса (.1) и не адрес сети (.0)


def test_addresses_go_on_past_the_last_octet() -> None:
    first_block = [IPv4Address("10.77.0.0") + offset for offset in range(2, 256)]  # .0.2 … .0.255
    config = with_peers(gateway_config(), first_block)
    assert pick_free_address(config) == IPv4Address("10.77.1.0")  # в /22 это обычный адрес клиента


def test_22_pool_gives_1021_addresses_then_is_exhausted() -> None:
    every_client = [IPv4Address("10.77.0.0") + offset for offset in range(2, 1023)]  # до 10.77.3.254 включительно
    assert len(every_client) == 1021 and every_client[-1] == IPv4Address("10.77.3.254")
    full = with_peers(gateway_config(), every_client)
    with pytest.raises(AddressPoolExhaustedError, match="10.77.0.0/22"):
        pick_free_address(full)  # broadcast 10.77.3.255, адрес сети и адрес интерфейса не выдаются

    hole = IPv4Address("10.77.2.7")
    config = with_peers(gateway_config(), [address for address in every_client if address != hole])
    assert pick_free_address(config) == hole


async def test_gateway_clients_get_consecutive_addresses_from_dot_two() -> None:
    fake = FakeServer(layout=GW)
    async with FakeSSHClient(fake) as ssh:
        provider = GatewayProvider(ssh)
        await provider.install()
        addresses = [(await provider.create_client(f"client{index}")).address for index in range(17)]
    assert addresses == [f"10.77.0.{last}" for last in range(2, 19)]  # 17-й клиент — .0.18
    assert "10.77.0.1" not in addresses and "10.77.0.0" not in addresses


async def test_gateway_pool_exhaustion_is_reported_by_the_provider() -> None:
    class TinyGateway(GatewayProvider):
        layout: ClassVar[ContainerLayout] = replace(GW, tunnel_subnet="10.77.0.0/29", server_address="10.77.0.1/29")

    fake = FakeServer(layout=TinyGateway.layout)
    async with FakeSSHClient(fake) as ssh:
        provider = TinyGateway(ssh)
        await provider.install()
        addresses = [(await provider.create_client(f"c{index}")).address for index in range(5)]
        assert addresses == ["10.77.0.2", "10.77.0.3", "10.77.0.4", "10.77.0.5", "10.77.0.6"]  # .7 — broadcast
        with pytest.raises(AddressPoolExhaustedError):
            await provider.create_client("one-too-many")
    assert len(list_peers(fake.files["/opt/vpn-admin/gw/gw0.conf"])) == 5  # неудачный клиент в конфиг не попал


def test_default_subnet_still_starts_at_dot_one() -> None:
    config = render_awg_config(generate_secret_keys().private_key, 31000)
    assert interface_values(config)["Address"] == "10.8.1.0/24"
    assert pick_free_address(config) == IPv4Address("10.8.1.1")


# --- (в) конфиг клиента, vpn://-ссылка, QR ---------------------------------------------------------------------


def client_config(server_config: str, dns: str | None = None) -> str:
    client = generate_secret_keys()
    extra = {} if dns is None else {"dns": dns}
    return render_client_config(
        server_config=server_config,
        client_private_key=client.private_key,
        client_address=IPv4Address("10.77.0.2"),
        psk=client.psk,
        server_public_key=generate_secret_keys().public_key,
        endpoint_host="203.0.113.10",
        **extra,
    )


def test_client_config_takes_dns_from_the_parameter_with_the_old_default() -> None:
    assert f"DNS = {CLIENT_DNS}\n" in client_config(gateway_config())
    custom = client_config(gateway_config(), dns="10.77.0.1")
    assert "DNS = 10.77.0.1\n" in custom and "1.0.0.1" not in custom


def test_link_carries_custom_subnet_and_dns() -> None:
    text = client_config(gateway_config(), dns="10.77.0.1")
    document = decode(amnezia_share_link(text, "GW", subnet="10.77.0.0/22"))
    assert document["containers"][0]["awg"]["subnet_address"] == "10.77.0.0"
    assert (document["dns1"], document["dns2"]) == ("10.77.0.1", "10.77.0.1")  # один DNS — он же запасной

    document = decode(amnezia_share_link(text, "GW", subnet="10.77.0.0/22", dns="10.77.0.1, 9.9.9.9"))
    assert (document["dns1"], document["dns2"]) == ("10.77.0.1", "9.9.9.9")  # параметр важнее строки конфига

    default = decode(amnezia_share_link(client_config(gateway_config()), "GW"))
    assert default["containers"][0]["awg"]["subnet_address"] == "10.8.1.0"  # прежнее значение по умолчанию
    assert (default["dns1"], default["dns2"]) == ("1.1.1.1", "1.0.0.1")


def test_gateway_provider_passes_its_layout_to_link_and_qr_as_a_classmethod() -> None:
    text = client_config(gateway_config(), dns=GW.client_dns)
    document = decode(GatewayProvider.share_link(text, "GW") or "")
    assert document["containers"][0]["awg"]["subnet_address"] == "10.77.0.0"
    assert (document["dns1"], document["dns2"]) == ("10.77.0.1", "10.77.0.1")

    qr = GatewayProvider.share_qr_codes(text, "GW")
    assert qr == amnezia_qr_codes(text, "GW", subnet="10.77.0.0/22", dns="10.77.0.1")
    chunks = []
    for code in qr or []:
        raw = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
        _, _, index, size = struct.unpack(">hBBI", raw[:8])
        chunks.append((index, raw[8 : 8 + size]))
    payload = b"".join(part for _, part in sorted(chunks))
    assert json.loads(zlib.decompress(payload[4:]))["containers"][0]["awg"]["subnet_address"] == "10.77.0.0"

    # Вызов через класс, как в modules/access/service.py: экземпляр не нужен.
    default_text = client_config(render_awg_config(generate_secret_keys().private_key, 31000))
    default_link = decode(AmneziaWGProvider.share_link(default_text, "US2") or "")
    assert default_link["containers"][0]["awg"]["subnet_address"] == "10.8.1.0"
    assert VPNProvider.share_link(default_text, "US2") is None


def test_nat_hooks_follow_the_subnet() -> None:
    old = "[Interface]\nPrivateKey = k\nAddress = 10.77.0.1/22\nListenPort = 1\n"
    patched, changed = ensure_nat_hooks(old, GW.tunnel_subnet)
    assert changed and f"PostUp = {GW.nat_post_up}" in patched and f"PostDown = {GW.nat_post_down}" in patched
    assert ensure_nat_hooks(patched, GW.tunnel_subnet) == (patched, False)
    again, changed_by_default = ensure_nat_hooks(patched)  # хук другой подсети для обычного — не «уже есть»
    assert changed_by_default and NAT_POST_UP in again


# --- (г) обычная раскладка не изменилась -----------------------------------------------------------------------


async def test_default_layout_commands_are_byte_for_byte_the_old_ones() -> None:
    """Журнал команд install → create_client → sync → health → install → revoke → uninstall снят с кода ДО
    введения раскладки. Любая правка текста команды обычного протокола должна ломать этот тест."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    fake = FakeServer()
    await full_flow(AmneziaWGProvider, fake)
    assert fake.commands == golden


def test_default_server_config_text_is_unchanged() -> None:
    obfuscation = Obfuscation(
        jc=5, jmin=10, jmax=50, s1=60, s2=77, h1=1020325451, h2=3288052141, h3=1766607858, h4=2528465083
    )
    rule = "POSTROUTING -s 10.8.1.0/24 -o eth0 -j MASQUERADE"
    expected = (
        "[Interface]\n"
        "PrivateKey = KEY\n"
        "Address = 10.8.1.0/24\n"
        "ListenPort = 31000\n"
        f"PostUp = iptables -t nat -C {rule} 2>/dev/null || iptables -t nat -A {rule}\n"
        f"PostDown = iptables -t nat -D {rule}\n"
        "Jc = 5\nJmin = 10\nJmax = 50\nS1 = 60\nS2 = 77\n"
        "H1 = 1020325451\nH2 = 3288052141\nH3 = 1766607858\nH4 = 2528465083\n"
        "S3 = 33\nS4 = 12\nContentPaddingAddition = 10-100\nRekeyAfterTime = 100-120\nRekeyTimeout = 3-7\n"
        "RejectAfterTime = 150-180\nKeepaliveTimeout = 5-15\nMaxHandshakeAttempts = 15-20\n"
        "RandomTrailers = on\nDisableCookies = on"
    )
    assert render_awg_config("KEY", 31000, obfuscation) == expected
