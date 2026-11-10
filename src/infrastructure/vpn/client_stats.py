"""Read only public peer statistics; `dump` must never be used (it includes secrets)."""

from infrastructure.ssh.client import SSHClient
from infrastructure.vpn.awg_config import is_valid_public_key, parse_client_transfers, parse_latest_handshakes
from infrastructure.vpn.provider import VPNClientStats


async def read_client_stats(ssh: SSHClient, container: str, tool: str, interface: str) -> list[VPNClientStats] | None:
    # All command fragments come from provider constants, never from an HTTP request or peer data.
    handshakes = await ssh.execute(f"docker exec {container} {tool} show {interface} latest-handshakes")
    if handshakes.exit_status != 0 or len(handshakes.stdout) > 1024 * 1024:
        return None
    peers = {
        key: value for key, value in parse_latest_handshakes(handshakes.stdout).items() if is_valid_public_key(key)
    }
    if len(peers) > 4096 or (handshakes.stdout.strip() and not peers):
        return None
    transfer = await ssh.execute(f"docker exec {container} {tool} show {interface} transfer")
    counters = parse_client_transfers(transfer.stdout) if transfer.exit_status == 0 else {}
    return [VPNClientStats(key, handshake, *counters.get(key, (None, None))) for key, handshake in peers.items()]
