"""Manual latest-image replacement must preserve existing VPN identity and be recoverable."""

from dataclasses import asdict
from ipaddress import IPv4Address, IPv4Network
from types import SimpleNamespace
from typing import Any

import pytest

from core.ssh_exception import DockerError, ForeignContainerError, UnsupportedOperationError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.amnezia_app import AmneziaAppAWG2Provider, AmneziaAppAWGProvider
from infrastructure.vpn.awg_config import list_peers
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT
from tests.fakes.fake_ssh import FakeSSHClient
from tests.fakes.protocol_update import DIGEST, LATEST, NEW_IMAGE, OLD_IMAGE, UpdateServer
from tests.gateway_support import add_live_exit


@pytest.fixture(autouse=True)
def no_update_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    from infrastructure.vpn import container_update

    async def instant(_: float) -> None:
        pass

    monkeypatch.setattr(container_update, "asyncio", SimpleNamespace(sleep=instant))


async def installed(gateway: bool = False) -> UpdateServer:
    fake = UpdateServer(**({"layout": GATEWAY_LAYOUT} if gateway else {}))
    cls = AmneziaGatewayProvider if gateway else AmneziaWGProvider
    async with FakeSSHClient(fake) as ssh:
        await cls(ssh).install()
        await cls(ssh).create_client("existing-phone")
    fake.image_ids[fake.layout.container_name] = OLD_IMAGE
    fake.commands.clear()
    return fake


async def update(fake: UpdateServer, gateway: bool = False) -> Any:
    async with FakeSSHClient(fake) as ssh:
        provider = (AmneziaGatewayProvider if gateway else AmneziaWGProvider)(ssh)
        method = getattr(provider, "update", None)
        assert callable(method), "VPNProvider.update() is required for manual container updates"
        return await method()


def changing_commands(fake: UpdateServer) -> list[str]:
    return [c for c in fake.commands if c.startswith(("docker stop", "docker rename", "docker rm", "docker run"))]


@pytest.mark.parametrize("gateway", [False, True], ids=["awg", "gateway"])
async def test_update_pins_latest_digest_preserves_files_port_and_peers(gateway: bool) -> None:
    fake = await installed(gateway)
    before, peers, ports = dict(fake.files), set(fake.interface_peers), dict(fake.container_ports)
    result = await update(fake, gateway)
    assert result.changed is True
    assert (result.previous_image_id, result.target_image_id, result.target_digest) == (OLD_IMAGE, NEW_IMAGE, DIGEST)
    assert result.rollback == "not_needed" and result.recovered is False
    assert fake.files == before and fake.interface_peers == peers
    assert fake.container_ports == ports and fake.running
    assert fake.image_ids[fake.layout.container_name] == NEW_IMAGE
    pull = fake.commands.index(f"docker pull {LATEST}")
    stop = next(i for i, cmd in enumerate(fake.commands) if cmd.startswith("docker stop"))
    assert pull < stop
    runs = [c for c in fake.commands if c.startswith("docker run")]
    assert len(runs) == 1 and DIGEST in runs[0] and LATEST not in runs[0]
    assert set(fake.containers) == {fake.layout.container_name}
    assert "PrivateKey" not in str(asdict(result))


async def test_identical_image_does_not_restart_vpn() -> None:
    fake = await installed()
    fake.target_image_id = OLD_IMAGE
    result = await update(fake)
    assert result.changed is False and result.target_image_id == OLD_IMAGE
    assert changing_commands(fake) == [] and fake.running


async def test_pull_failure_keeps_working_container_and_config() -> None:
    fake = await installed()
    fake.pull_fails = True
    before = dict(fake.files)
    with pytest.raises(DockerError) as error:
        await update(fake)
    assert "SECRET-MARKER" not in str(error.value)
    assert fake.files == before and fake.running and changing_commands(fake) == []


@pytest.mark.parametrize("digest", ["evil/repository@sha256:" + "b" * 64, "amneziavpn/amneziawg-go:latest", "$(id)"])
async def test_untrusted_or_malformed_digest_never_stops_vpn(digest: str) -> None:
    fake = await installed()
    fake.target_digest = digest
    with pytest.raises(DockerError):
        await update(fake)
    assert fake.running and changing_commands(fake) == []


async def test_foreign_container_is_rejected_without_pull_or_mutation() -> None:
    fake = await installed()
    fake.mounts[fake.layout.container_name] = ["/opt/amnezia/awg"]
    with pytest.raises(ForeignContainerError):
        await update(fake)
    assert changing_commands(fake) == [] and not any(c.startswith("docker pull") for c in fake.commands)


async def test_failed_new_container_restores_whole_config_directory_and_previous_image() -> None:
    fake = await installed()
    fake.files[f"{fake.layout.host_config_dir}/preserved-extra.conf"] = "existing private config\n"
    before, peers = dict(fake.files), set(fake.interface_peers)
    fake.fail_new_start = fake.mutate_config_on_start = True
    with pytest.raises(DockerError) as error:
        await update(fake)
    assert fake.files == before and fake.interface_peers == peers and fake.running
    assert fake.image_ids[fake.layout.container_name] == OLD_IMAGE
    assert error.value.job_result["rollback"] == "succeeded"  # type: ignore[attr-defined]
    assert "existing private config" not in str(error.value)


async def test_repeated_update_recovers_after_worker_dies_during_replace() -> None:
    fake = await installed()
    before = dict(fake.files)
    fake.crash_after_rename = True
    with pytest.raises(KeyboardInterrupt, match="simulated worker death"):
        await update(fake)
    result = await update(fake)
    assert result.recovered is True
    assert fake.files == before and fake.running
    assert set(fake.containers) == {fake.layout.container_name}


async def test_failed_rollback_is_reported_and_preserves_recovery_metadata() -> None:
    fake = await installed()
    fake.fail_new_start = fake.fail_old_start = True
    with pytest.raises(DockerError) as error:
        await update(fake)
    assert error.value.job_result["rollback"] == "failed"  # type: ignore[attr-defined]
    assert f"{fake.layout.host_config_dir}/.container-update.json" in fake.files
    assert fake.layout.host_config_dir + "-update-snapshot" in fake.dirs


async def test_running_new_image_that_changes_config_is_rolled_back() -> None:
    fake = await installed()
    before = dict(fake.files)
    fake.mutate_config_on_start = True
    with pytest.raises(DockerError) as error:
        await update(fake)
    assert error.value.job_result["rollback"] == "succeeded"  # type: ignore[attr-defined]
    assert fake.files == before and fake.running
    assert fake.image_ids[fake.layout.container_name] == OLD_IMAGE


async def test_recovery_refuses_backup_container_with_unexpected_id() -> None:
    fake = await installed()
    fake.crash_after_rename = True
    with pytest.raises(KeyboardInterrupt):
        await update(fake)
    backup = fake.layout.container_name + "-update-backup"
    fake.container_ids[backup] = "9" * 64
    before = dict(fake.files)
    fake.commands.clear()
    with pytest.raises(DockerError):
        await update(fake)
    assert changing_commands(fake) == []
    assert fake.container_ids[backup] == "9" * 64 and fake.files == before


async def test_unknown_backup_without_journal_is_not_removed_or_overwritten() -> None:
    fake = await installed()
    backup = fake.layout.container_name + "-update-backup"
    fake.containers[backup] = "exited"
    fake.container_ids[backup] = "9" * 64
    with pytest.raises(DockerError):
        await update(fake)
    assert changing_commands(fake) == [] and fake.running
    assert not any(command.startswith("docker pull") for command in fake.commands)


async def test_gateway_update_restores_exits_assignments_and_russian_routes_without_fresh_handshake() -> None:
    fake = await installed(gateway=True)
    await add_live_exit(fake, handshake=0)
    address = IPv4Address(list_peers(fake.files[fake.layout.host_config_file])[0].address)
    async with FakeSSHClient(fake) as ssh:
        instance = AmneziaGatewayProvider(ssh)
        await instance.apply_assignments({address: 1})
        await instance.apply_ru([IPv4Network("5.45.200.0/24")])
        before_runtime = await instance._update_runtime_state()
    before_files = dict(fake.files)
    result = await update(fake, gateway=True)
    async with FakeSSHClient(fake) as ssh:
        after_runtime = await AmneziaGatewayProvider(ssh)._update_runtime_state()
    assert result.changed and fake.files == before_files
    assert after_runtime == before_runtime
    assert fake.exit_interfaces == ["gwx1"] and fake.assign_chain == {str(address): 1001}
    assert any(rule.startswith("9000:") and "prohibit" in rule for rule in fake.ip_rules)


@pytest.mark.parametrize("cls", [AmneziaAppAWGProvider, AmneziaAppAWG2Provider])
async def test_app_container_update_is_unsupported_and_never_touches_ssh(cls: Any) -> None:
    fake = UpdateServer()
    async with FakeSSHClient(fake) as ssh:
        method = getattr(cls(ssh), "update", None)
        assert callable(method), "Base provider must explicitly reject unsupported updates"
        with pytest.raises(UnsupportedOperationError):
            await method()
    assert fake.commands == [] and fake.reads == [] and fake.writes == []
