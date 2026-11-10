"""Read-only per-peer counters: malformed remote input, ownership guards and no config reads."""

from datetime import datetime, timezone

import pytest

from infrastructure.ssh.client import CommandResult
from infrastructure.vpn import awg_config
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.amnezia_app import AmneziaAppAWG2Provider, AmneziaAppAWGProvider
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from tests.fakes.fake_ssh import AppPeer, FakeServer, FakeSSHClient


def key() -> str:
    return awg_config.generate_secret_keys().public_key


def test_transfer_parser_preserves_rx_tx_and_zero_counters() -> None:
    first, second = key(), key()
    result = awg_config.parse_client_transfers(f"{first}\t123\t456\n{second}\t0\t0\n")
    assert result == {first: (123, 456), second: (0, 0)}


@pytest.mark.parametrize("counters", ["-1 5", "5 -1", "a 5", "5", "5 8 9", f"{2**63} 0", "0 1.5"])
def test_transfer_parser_skips_bad_rows_without_losing_valid_peers(counters: str) -> None:
    good, bad = key(), key()
    assert awg_config.parse_client_transfers(f"{good} 1 2\n{bad} {counters}\n") == {good: (1, 2)}


def test_transfer_parser_rejects_invalid_keys_and_preserves_valid_duplicate() -> None:
    public_key = key()
    output = f"invalid-key 1 2\n{public_key} 3 4\n{public_key} -1 9\n"
    assert awg_config.parse_client_transfers(output) == {public_key: (3, 4)}


def test_transfer_parser_rejects_oversized_output() -> None:
    assert awg_config.parse_client_transfers("x" * (1024 * 1024 + 1)) == {}


def test_transfer_parser_bounds_the_number_of_remote_rows() -> None:
    first, ignored = key(), key()
    output = f"{first} 1 2\n" * 4096 + f"{ignored} 3 4\n"
    assert awg_config.parse_client_transfers(output) == {first: (1, 2)}


@pytest.mark.parametrize("provider_type", [AmneziaWGProvider, AmneziaGatewayProvider])
async def test_owned_stats_use_runtime_peers_without_reading_configs(provider_type: type[AmneziaWGProvider]) -> None:
    public_key, never = key(), key()
    layout = provider_type.layout
    fake = FakeServer(layout=layout, containers={layout.container_name: "running"})
    fake.files[layout.host_config_file] = "private config must not be read"
    fake.interface_peers = {public_key, never}
    fake.handshakes[public_key] = 1_700_000_000
    fake.transfers[public_key] = (123, 456)

    async with FakeSSHClient(fake) as ssh:
        rows = await provider_type(ssh).client_stats()

    assert rows is not None
    peers = {row.public_key: row for row in rows}
    assert set(peers) == {public_key, never}
    assert peers[public_key].latest_handshake == datetime.fromtimestamp(1_700_000_000, timezone.utc)
    assert (peers[public_key].rx_bytes, peers[public_key].tx_bytes) == (123, 456)
    assert peers[never].latest_handshake is None
    assert (peers[never].rx_bytes, peers[never].tx_bytes) == (0, 0)
    assert fake.reads == [] and fake.writes == []
    assert not any("dump" in command or "cat " in command for command in fake.commands)


async def test_owned_provider_never_reads_stats_from_an_app_container() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg", peers=[AppPeer(key())])
    async with FakeSSHClient(fake) as ssh:
        assert await AmneziaWGProvider(ssh).client_stats() is None
    assert not any(command.startswith("docker exec") for command in fake.commands)


async def test_app_provider_never_reads_stats_from_a_panel_owned_container() -> None:
    fake = FakeServer(containers={AmneziaWGProvider.layout.container_name: "running"})
    async with FakeSSHClient(fake) as ssh:
        assert await AmneziaAppAWGProvider(ssh).client_stats() is None
    assert not any(command.startswith("docker exec") for command in fake.commands)


@pytest.mark.parametrize("provider_type", [AmneziaAppAWGProvider, AmneziaAppAWG2Provider])
async def test_app_stats_read_only_runtime_fields(provider_type: type[AmneziaAppAWGProvider]) -> None:
    public_key = key()
    fake = FakeServer()
    app = fake.add_amnezia_app(provider_type.container, peers=[AppPeer(public_key, handshake=1_700_000_000)])
    app.transfers[public_key] = (12, 34)
    async with FakeSSHClient(fake) as ssh:
        rows = await provider_type(ssh).client_stats()
    assert rows is not None and len(rows) == 1
    assert (rows[0].public_key, rows[0].rx_bytes, rows[0].tx_bytes) == (public_key, 12, 34)
    assert rows[0].latest_handshake == datetime.fromtimestamp(1_700_000_000, timezone.utc)
    assert fake.container_reads == [] and fake.changing_commands() == []
    assert not any("dump" in command for command in fake.commands)


@pytest.mark.parametrize("field", ["latest-handshakes", "transfer"])
async def test_failed_runtime_command_is_unavailable_instead_of_zero(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    fake = FakeServer(containers={AmneziaWGProvider.layout.container_name: "running"})
    fake.files[AmneziaWGProvider.layout.host_config_file] = "private config must not be read"
    fake.interface_peers = {key()}
    original = FakeSSHClient.execute

    async def execute(self: FakeSSHClient, command: str, *args: object, **kwargs: object) -> CommandResult:
        if command.endswith(f" {field}"):
            return CommandResult(stdout="", stderr="runtime query failed", exit_status=1)
        return await original(self, command, *args, **kwargs)

    monkeypatch.setattr(FakeSSHClient, "execute", execute)
    async with FakeSSHClient(fake) as ssh:
        rows = await AmneziaWGProvider(ssh).client_stats()
    if field == "latest-handshakes":
        assert rows is None
    else:
        assert rows is not None and len(rows) == 1
        assert rows[0].rx_bytes is None and rows[0].tx_bytes is None


async def test_successful_empty_runtime_is_an_empty_sample() -> None:
    fake = FakeServer(containers={AmneziaWGProvider.layout.container_name: "running"})
    fake.files[AmneziaWGProvider.layout.host_config_file] = "private config must not be read"
    async with FakeSSHClient(fake) as ssh:
        assert await AmneziaWGProvider(ssh).client_stats() == []
