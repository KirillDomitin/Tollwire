"""Общее для тестов выходов шлюза (этап 24.2): конфиг внутреннего клиента, туннель, «живой» выход на фейковом шлюзе."""

import time
from collections.abc import Sequence

from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import ExitTunnel
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient

EXIT_ENDPOINT = "198.51.100.7:34567"


async def client_config(name: str = "gw-0123456789ab", host: str = "198.51.100.7") -> str:
    """Конфиг внутреннего клиента на настоящем AmneziaWGProvider поверх отдельного фейкового выхода."""
    fake = FakeServer(host=host)
    async with FakeSSHClient(fake) as ssh:
        provider = AmneziaWGProvider(ssh)
        await provider.install()
        return (await provider.create_client(name)).config


async def exit_tunnel(slot: int = 1, endpoint: str = EXIT_ENDPOINT, config: str | None = None) -> ExitTunnel:
    return ExitTunnel(slot=slot, client_config=config or await client_config(), endpoint=endpoint)


async def apply_exits(fake: FakeServer, tunnels: Sequence[ExitTunnel]) -> None:
    async with FakeSSHClient(fake) as ssh:
        await AmneziaGatewayProvider(ssh).apply_exits(tunnels)


def fresh_handshake() -> int:
    return int(time.time()) - 5


async def add_live_exit(fake: FakeServer, slot: int = 1, *, handshake: int | None = None) -> ExitTunnel:
    """Поднимает на фейковом шлюзе исправный выход: файл, интерфейс и свежее рукопожатие."""
    tunnel = await exit_tunnel(slot)
    await apply_exits(fake, [tunnel])
    fake.exit_handshakes[slot] = fresh_handshake() if handshake is None else handshake
    return tunnel
