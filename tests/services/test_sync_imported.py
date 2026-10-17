"""sync и импортированные клиенты (этап 23.4): у импортированного credential нет конфига, зато есть PSK, адрес и ключ —
поэтому пропавший с сервера peer возвращается через `restore_peer`, а не `restore_client`. Чужие peer'ы приложения
(в том числе с именем grant-…) sync не трогает; ISSUED-credential ведёт себя как раньше."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from core.security import encrypt_secret
from core.ssh_exception import CommandExecutionError, UnsupportedOperationError
from infrastructure.vpn.awg_config import list_peers
from modules.access.models import AccessGrantStatus
from modules.access.models.vpn_credential import VPNCredential
from modules.access.service import client_name
from modules.servers.sync import ServerSyncService
from tests.fakes.fake_ssh import APP_CLIENTS_TABLE, AppPeer
from tests.fixtures import World
from tests.import_support import (
    PROTOCOL_TYPE,
    Scene,
    add_app_protocol,
    drop_peer,
    imported_credential,
    make_scene,
    new_key,
)


async def synced(world: World, scene: Scene) -> dict[str, Any]:
    report = await ServerSyncService(world.session).sync(scene.server.id)
    return {p["type"]: p for p in report["protocols"]}[PROTOCOL_TYPE]  # type: ignore[no-any-return]


def keys(items: list[dict[str, Any]]) -> set[str]:
    return {item["public_key"] for item in items}


async def tracked(world: World, *, count: int = 2) -> tuple[Scene, Any]:
    scene = await make_scene(world, count=count)
    return scene, await add_app_protocol(world, scene)


# --- возврат peer'а ---


async def test_sync_returns_the_missing_peer_of_an_imported_client_with_its_psk_and_address(world: World) -> None:
    scene, protocol = await tracked(world)
    key, other = scene.keys
    psk = scene.app.peers[0].psk
    _, _, grant, credential = await imported_credential(world, protocol, key, psk=psk, address="10.8.1.2")
    drop_peer(scene.app, key)  # пользователь клиента удалил его в приложении (или приложение переустановили)
    other_before = [p for p in scene.app.peers if p.public_key == other]

    entry = await synced(world, scene)

    assert keys(entry["restored"]) == {key} and entry["restored"][0]["credential_id"] == str(credential.id)
    assert entry["issues"] == []
    (peer,) = [p for p in scene.app.peers if p.public_key == key]  # peer вернулся и в живой интерфейс
    assert (peer.psk, peer.allowed_ips) == (psk, "10.8.1.2/32")
    config = scene.app.files[scene.app.config_path]
    assert f"PublicKey = {key}" in config and f"PresharedKey = {psk}" in config
    assert [p.public_key for p in list_peers(config)] == [other, key]
    assert [p for p in scene.app.peers if p.public_key == other] == other_before  # чужой peer не тронут
    names = {e["clientId"]: e["userData"]["clientName"] for e in json.loads(scene.app.files[APP_CLIENTS_TABLE])}
    assert names[key] == client_name(grant.id)  # имя для приложения — по гранту панели
    assert scene.fake.unexpected_changing_commands() == [] and scene.fake.stage_files() == []


async def test_sync_report_and_logs_about_a_restored_peer_have_no_secrets(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    scene, protocol = await tracked(world)
    await imported_credential(world, protocol, scene.keys[0], psk=scene.app.peers[0].psk)
    secrets = list(scene.app.secrets)
    drop_peer(scene.app, scene.keys[0])
    caplog.set_level("DEBUG")
    report = await ServerSyncService(world.session).sync(scene.server.id)
    text = json.dumps(report) + caplog.text
    assert not any(secret in text for secret in secrets)
    assert "PresharedKey" not in text and "PrivateKey" not in text


async def test_second_sync_finds_the_returned_peer_and_changes_nothing(world: World) -> None:
    scene, protocol = await tracked(world)
    key = scene.keys[0]
    await imported_credential(world, protocol, key, psk=scene.app.peers[0].psk)
    drop_peer(scene.app, key)
    await synced(world, scene)
    copies, applies, config = len(scene.fake.copies), scene.app.applies, scene.app.files[scene.app.config_path]

    entry = await synced(world, scene)
    assert entry["restored"] == [] and key in keys(entry["ok"])
    assert (len(scene.fake.copies), scene.app.applies) == (copies, applies)
    assert scene.app.files[scene.app.config_path] == config


async def test_live_imported_client_present_on_the_server_is_ok_and_nothing_is_written(world: World) -> None:
    scene, protocol = await tracked(world)
    await imported_credential(world, protocol, scene.keys[0], psk=scene.app.peers[0].psk)
    entry = await synced(world, scene)
    assert scene.keys[0] in keys(entry["ok"]) and entry["restored"] == []
    assert scene.fake.changing_commands() == [] and scene.fake.writes == [] and scene.fake.copies == []


async def test_imported_client_without_a_stored_psk_is_skipped_with_an_issue(world: World) -> None:
    scene, protocol = await tracked(world)
    key = scene.keys[0]
    _, _, _, credential = await imported_credential(world, protocol, key, psk=None)
    drop_peer(scene.app, key)
    scene.fake.writes.clear()

    entry = await synced(world, scene)  # sync не падает
    assert entry["restored"] == []
    assert any("no PSK stored" in issue for issue in entry["issues"]), entry["issues"]
    assert key not in scene.app.files[scene.app.config_path]
    assert scene.fake.writes == [] and scene.fake.copies == [] and scene.fake.changing_commands() == []
    await world.session.refresh(credential)
    assert credential.revoked_at is None  # credential остаётся живым: решать админу


@pytest.mark.parametrize("error", [CommandExecutionError, ValueError, UnsupportedOperationError])
async def test_failure_to_return_one_peer_is_an_issue_and_does_not_break_the_sync(
    error: type[Exception], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infrastructure.vpn.amnezia_app import AmneziaAppAWG2Provider

    scene, protocol = await tracked(world, count=3)
    bad, good, _ = scene.keys
    await imported_credential(world, protocol, bad, psk=scene.app.peers[0].psk, address="10.8.1.2")
    await imported_credential(world, protocol, good, psk=scene.app.peers[1].psk, address="10.8.1.3")
    drop_peer(scene.app, bad)
    drop_peer(scene.app, good)
    original = AmneziaAppAWG2Provider.restore_peer

    async def flaky(self: Any, name: str, public_key: str, psk: str, address: str) -> bool:
        if public_key == bad:
            raise error("cannot restore this one")
        return await original(self, name, public_key, psk, address)

    monkeypatch.setattr(AmneziaAppAWG2Provider, "restore_peer", flaky)
    entry = await synced(world, scene)
    assert keys(entry["restored"]) == {good}  # остальные возвращаются
    assert any("cannot restore this one" in issue for issue in entry["issues"]), entry["issues"]
    assert bad not in scene.app.files[scene.app.config_path]


async def test_address_taken_by_another_peer_is_an_issue_and_the_other_peer_is_kept(world: World) -> None:
    scene, protocol = await tracked(world, count=2)
    gone, squatter = scene.keys
    await imported_credential(
        world, protocol, gone, psk=scene.app.peers[0].psk, address="10.8.1.3"
    )  # занят вторым peer'ом
    drop_peer(scene.app, gone)
    secrets = list(scene.app.secrets)

    entry = await synced(world, scene)
    assert entry["restored"] == [] and entry["issues"]
    assert not any(secret in " ".join(entry["issues"]) for secret in secrets)
    assert [p.public_key for p in scene.app.peers] == [squatter]
    assert gone not in scene.app.files[scene.app.config_path]


async def test_address_outside_the_subnet_is_an_issue(world: World) -> None:
    scene, protocol = await tracked(world, count=1)
    key = scene.keys[0]
    await imported_credential(world, protocol, key, psk=scene.app.peers[0].psk, address="192.168.77.7")
    drop_peer(scene.app, key)
    entry = await synced(world, scene)
    assert entry["restored"] == [] and entry["issues"]
    assert scene.fake.copies == [] and key not in scene.app.files[scene.app.config_path]


async def test_stopped_container_means_no_restore(world: World) -> None:
    scene, protocol = await tracked(world, count=1)
    await imported_credential(world, protocol, scene.keys[0], psk=scene.app.peers[0].psk)
    drop_peer(scene.app, scene.keys[0])
    scene.fake.containers["amnezia-awg2"] = "exited"
    entry = await synced(world, scene)
    assert entry["state"] == "stopped" and not entry.get("restored")
    assert any("not running" in issue for issue in entry["issues"])
    assert scene.fake.changing_commands() == [] and scene.fake.copies == []


# --- ISSUED рядом с IMPORTED ---


async def test_issued_credential_of_an_app_protocol_still_goes_through_restore_client(world: World) -> None:
    """Как раньше: restore_client приложения не поддерживается — issue, не падение; импортированный рядом вернётся."""
    scene, protocol = await tracked(world, count=3)
    issued_key, imported_key, _ = scene.keys
    _, _, grant, _ = await imported_credential(
        world, protocol, imported_key, psk=scene.app.peers[1].psk, address="10.8.1.3"
    )
    user, subscription, issued_grant = await world.access(protocol)
    issued_grant.status = AccessGrantStatus.ACTIVE
    issued = VPNCredential(
        access_grant_id=issued_grant.id,
        encrypted_config=encrypt_secret("[Interface]\nPrivateKey = client-side-secret\n"),
        client_address="10.8.1.2",
        client_public_key=issued_key,
    )
    world.session.add(issued)
    await world.session.commit()
    drop_peer(scene.app, issued_key)
    drop_peer(scene.app, imported_key)

    entry = await synced(world, scene)
    assert keys(entry["restored"]) == {imported_key}
    assert any(str(issued.id) in issue and "not restored" in issue for issue in entry["issues"]), entry["issues"]
    assert issued_key not in scene.app.files[scene.app.config_path]
    assert "client-side-secret" not in json.dumps(entry)


# --- отзыв и чужие peer'ы ---


async def test_revoked_imported_credential_whose_peer_is_still_on_the_server_is_removed(world: World) -> None:
    scene, protocol = await tracked(world)
    key, other = scene.keys
    _, _, _, credential = await imported_credential(world, protocol, key, psk=scene.app.peers[0].psk)
    credential.revoked_at = datetime.now(timezone.utc)
    await world.session.commit()

    entry = await synced(world, scene)
    assert keys(entry["removed_revoked"]) == {key}
    assert [p.public_key for p in scene.app.peers] == [other] and key not in scene.app.files[scene.app.config_path]
    assert scene.fake.unexpected_changing_commands() == []


async def test_imported_credential_of_an_ended_access_is_revoked_by_sync_and_its_peer_removed(world: World) -> None:
    scene, protocol = await tracked(world)
    key, other = scene.keys
    _, subscription, grant, credential = await imported_credential(world, protocol, key, psk=scene.app.peers[0].psk)
    subscription.valid_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    subscription.valid_from = datetime.now(timezone.utc) - timedelta(days=2)
    await world.session.commit()  # срок вышел, а REVOKE_ACCESS не дошёл

    entry = await synced(world, scene)
    assert [item["credential_id"] for item in entry["revocations_completed"]] == [str(credential.id)]
    await world.session.refresh(credential)
    assert credential.revoked_at is not None
    assert [p.public_key for p in scene.app.peers] == [other]


async def test_sync_does_not_touch_foreign_peers_of_a_tracked_app_even_with_grant_names(world: World) -> None:
    scene, protocol = await tracked(world, count=1)
    await imported_credential(world, protocol, scene.keys[0], psk=scene.app.peers[0].psk)
    orphan, stranger = new_key(), new_key()
    scene.app.peers += [AppPeer(orphan, "10.8.1.50/32"), AppPeer(stranger, "10.8.1.51/32")]
    scene.app.files[scene.app.config_path] = scene.app.render_config()
    scene.app.files[APP_CLIENTS_TABLE] = json.dumps(
        [
            {"clientId": orphan, "userData": {"clientName": "grant-deadbeef0000"}}
        ]  # имя «как у панели», но в БД клиента нет
    )
    config = scene.app.files[scene.app.config_path]

    entry = await synced(world, scene)
    assert keys(entry["foreign"]) == {orphan, stranger} and entry["removed_orphans"] == []
    assert scene.app.files[scene.app.config_path] == config and orphan in config and stranger in config
    assert scene.fake.changing_commands() == []
