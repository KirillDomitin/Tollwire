"""Чтение peer'ов для импорта (этап 23.4): `VPNProvider.read_importable()` и структура `ImportablePeer`.

Провайдеры приложения AmneziaVPN отдают ВСЕХ peer'ов конфига с PSK (он нужен, чтобы позже вернуть peer на сервер) и
именем из clientsTable. Только чтение: ни одной изменяющей команды, ни одного файла. PSK не попадает в repr, исключения
и логи. Новые имена берутся внутри хелперов: пока кода нет, красными становятся тесты, а не вся коллекция."""

import dataclasses
import importlib
import logging
from typing import Any

import pytest

from core.ssh_exception import CommandExecutionError, UnsupportedOperationError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.awg_config import generate_secret_keys
from infrastructure.vpn.provider import VPNProvider
from tests.fakes.fake_ssh import (
    APP_CLIENTS_TABLE,
    APP_PRIVATE_KEY_FILE,
    APP_PSK_FILE,
    AppPeer,
    FakeServer,
    FakeSSHClient,
)
from tests.import_support import strip_psk

CLASS_NAMES = {"amnezia-awg": "AmneziaAppAWGProvider", "amnezia-awg2": "AmneziaAppAWG2Provider"}


@pytest.fixture(params=sorted(CLASS_NAMES))
def container(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def provider_for(container: str, ssh: FakeSSHClient) -> VPNProvider:
    module = importlib.import_module("infrastructure.vpn.amnezia_app")
    return getattr(module, CLASS_NAMES[container])(ssh)  # type: ignore[no-any-return]


async def read(fake: FakeServer, container: str) -> list[Any]:
    async with FakeSSHClient(fake) as ssh:
        return await provider_for(container, ssh).read_importable()  # type: ignore[attr-defined, no-any-return]


def importable_class() -> Any:
    cls = getattr(importlib.import_module("infrastructure.vpn.provider"), "ImportablePeer", None)
    assert cls is not None, "infrastructure.vpn.provider.ImportablePeer is not defined"
    return cls


def key() -> str:
    return generate_secret_keys().public_key


# --- состав кода ---


def test_importable_peer_is_a_frozen_dataclass_with_the_agreed_fields() -> None:
    cls = importable_class()
    assert dataclasses.is_dataclass(cls)
    assert [f.name for f in dataclasses.fields(cls)] == ["public_key", "address", "psk", "name", "supported"]
    peer = cls(public_key="k", address="10.8.1.2", psk="secret-psk", name="Phone", supported=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        peer.name = "changed"


def test_importable_peer_repr_hides_the_psk() -> None:
    peer = importable_class()(
        public_key="k", address="10.8.1.2", psk="very-secret-psk-value", name="Phone", supported=True
    )
    assert "very-secret-psk-value" not in repr(peer) and "very-secret-psk-value" not in str(peer)
    assert "Phone" in repr(peer)  # остальное в repr остаётся для отладки


def test_read_importable_is_optional_for_providers() -> None:
    assert "read_importable" not in VPNProvider.__abstractmethods__  # type: ignore[attr-defined]
    assert hasattr(VPNProvider, "read_importable")


async def test_own_provider_does_not_support_import_and_runs_no_commands() -> None:
    fake = FakeServer()
    async with FakeSSHClient(fake) as ssh:
        with pytest.raises(UnsupportedOperationError):
            await AmneziaWGProvider(ssh).read_importable()  # type: ignore[attr-defined]
    assert fake.commands == []


# --- что возвращается ---


async def test_read_importable_returns_every_peer_with_psk_name_and_address(container: str) -> None:
    named, nameless = key(), key()
    fake = FakeServer()
    app = fake.add_amnezia_app(
        container,
        peers=[AppPeer(named, "10.8.1.2/32"), AppPeer(nameless, "10.8.1.3/32")],
        clients_table={named: "Phone"},
    )
    peers = await read(fake, container)
    assert [p.public_key for p in peers] == [named, nameless]
    by_key = {p.public_key: p for p in peers}
    assert (by_key[named].address, by_key[named].name, by_key[named].supported) == ("10.8.1.2", "Phone", True)
    assert (by_key[nameless].address, by_key[nameless].name, by_key[nameless].supported) == ("10.8.1.3", None, True)
    assert by_key[named].psk == app.peers[0].psk and by_key[nameless].psk == app.peers[1].psk


async def test_read_importable_marks_peers_with_several_addresses_as_unsupported(container: str) -> None:
    wide, normal = key(), key()
    fake = FakeServer()
    fake.add_amnezia_app(container, peers=[AppPeer(wide, "10.8.1.2/32, 10.8.1.9/32"), AppPeer(normal, "10.8.1.3/32")])
    by_key = {p.public_key: p for p in await read(fake, container)}
    assert (
        by_key[wide].supported is False and by_key[wide].address is None
    )  # peer возвращается, но импортировать нельзя
    assert by_key[normal].supported is True


async def test_peer_without_preshared_key_has_psk_none(container: str) -> None:
    with_psk, without = key(), key()
    fake = FakeServer()
    app = fake.add_amnezia_app(container, peers=[with_psk, without])
    strip_psk(app, without)
    by_key = {p.public_key: p for p in await read(fake, container)}
    assert by_key[without].psk is None  # не пустая строка: «PSK нет»
    assert by_key[with_psk].psk is not None


async def test_peer_with_empty_preshared_key_has_psk_none(container: str) -> None:
    empty = key()
    fake = FakeServer()
    fake.add_amnezia_app(container, peers=[AppPeer(empty, "10.8.1.2/32", psk="")])
    (peer,) = await read(fake, container)
    assert peer.psk is None


async def test_names_come_from_clients_table_in_both_formats(container: str) -> None:
    a, b = key(), key()
    fake = FakeServer()
    fake.add_amnezia_app(container, peers=[a, b], clients_table={a: "Alpha", b: "Beta"})
    assert {p.public_key: p.name for p in await read(fake, container)} == {a: "Alpha", b: "Beta"}

    legacy = FakeServer()
    app = legacy.add_amnezia_app(container, peers=[a])
    app.files[APP_CLIENTS_TABLE] = '{"%s": {"clientName": "Old format"}}' % a
    assert [p.name for p in await read(legacy, container)] == ["Old format"]


async def test_names_are_cleaned_and_limited_like_in_the_discovery(container: str) -> None:
    a = key()
    fake = FakeServer()
    fake.add_amnezia_app(container, peers=[a], clients_table={a: "Ph\x00one‮" + "x" * 300})
    (peer,) = await read(fake, container)
    assert peer.name is not None and len(peer.name) <= 100
    assert "\x00" not in peer.name and "‮" not in peer.name


async def test_unreadable_clients_table_gives_peers_without_names(container: str) -> None:
    a = key()
    fake = FakeServer()
    app = fake.add_amnezia_app(container, peers=[a], clients_table={a: "Phone"})
    app.files[APP_CLIENTS_TABLE] = "not json at all"
    (peer,) = await read(fake, container)
    assert (peer.public_key, peer.name) == (a, None)

    del app.files[APP_CLIENTS_TABLE]
    (peer,) = await read(fake, container)
    assert (peer.public_key, peer.name) == (a, None)


async def test_peers_with_invalid_public_key_are_skipped(container: str) -> None:
    good = key()
    fake = FakeServer()
    app = fake.add_amnezia_app(container, peers=[good])
    app.files[app.config_path] += "\n[Peer]\nPublicKey = not-a-key\nAllowedIPs = 10.8.1.50/32\n"
    assert [p.public_key for p in await read(fake, container)] == [good]


async def test_config_without_peers_gives_an_empty_list(container: str) -> None:
    fake = FakeServer()
    fake.add_amnezia_app(container, peers=[])
    assert await read(fake, container) == []


# --- только чтение, секреты ---


async def test_read_importable_changes_nothing_and_reads_only_the_config_and_clients_table(container: str) -> None:
    a = key()
    fake = FakeServer()
    app = fake.add_amnezia_app(container, peers=[a], clients_table={a: "Phone"})
    files_before = dict(app.files)
    await read(fake, container)
    assert fake.changing_commands() == [] and fake.writes == [] and fake.copies == []
    assert app.files == files_before and app.applies == 0
    read_paths = {path for _, path in fake.container_reads}
    assert read_paths <= {app.config_path, APP_CLIENTS_TABLE}
    assert APP_PRIVATE_KEY_FILE not in read_paths and APP_PSK_FILE not in read_paths  # секретные файлы не читали


async def test_read_importable_does_not_log_or_leak_secrets(container: str, caplog: pytest.LogCaptureFixture) -> None:
    a = key()
    fake = FakeServer()
    app = fake.add_amnezia_app(container, peers=[a, key()], clients_table={a: "Phone"})
    caplog.set_level(logging.DEBUG)
    peers = await read(fake, container)
    for secret in app.secrets:
        assert secret not in caplog.text
        assert all(secret not in repr(peer) for peer in peers)
    assert "PrivateKey" not in caplog.text and "PresharedKey" not in caplog.text


# --- контейнер не готов: ошибка, а не «клиентов нет» ---


async def test_stopped_container_is_a_command_error_and_not_an_empty_list(container: str) -> None:
    fake = FakeServer()
    app = fake.add_amnezia_app(container, peers=[key()], running=False)
    with pytest.raises(CommandExecutionError, match="not running") as raised:
        await read(fake, container)
    assert all(secret not in str(raised.value) for secret in app.secrets)
    assert not any(c.startswith("docker exec") for c in fake.commands)  # в остановленный контейнер не ходим
    assert fake.changing_commands() == []


async def test_missing_container_is_a_command_error(container: str) -> None:
    fake = FakeServer()
    with pytest.raises(CommandExecutionError):
        await read(fake, container)
    assert not any(c.startswith("docker exec") for c in fake.commands)


async def test_panel_own_container_is_not_read_for_import() -> None:
    """Контейнер amnezia-awg, поставленный самой панелью, — не приложение: за чужим конфигом в него не ходим."""
    fake = FakeServer()
    async with FakeSSHClient(fake) as ssh:
        await AmneziaWGProvider(ssh).install()
    fake.commands.clear()
    with pytest.raises(CommandExecutionError):
        await read(fake, "amnezia-awg")
    assert not any(c.startswith("docker exec") for c in fake.commands)


async def test_unreadable_config_is_a_command_error_without_secrets(container: str) -> None:
    fake = FakeServer()
    app = fake.add_amnezia_app(container, peers=[key()])
    secrets = list(app.secrets)
    del app.files[app.config_path]
    with pytest.raises(CommandExecutionError) as raised:
        await read(fake, container)
    assert all(secret not in str(raised.value) for secret in secrets)


async def test_v1_provider_does_not_read_the_v2_container() -> None:
    v1_key, v2_key = key(), key()
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg", peers=[v1_key])
    fake.add_amnezia_app("amnezia-awg2", peers=[v2_key])
    assert [p.public_key for p in await read(fake, "amnezia-awg")] == [v1_key]
    assert [p.public_key for p in await read(fake, "amnezia-awg2")] == [v2_key]
