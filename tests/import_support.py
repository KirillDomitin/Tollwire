"""Общее для тестов импорта клиентов из AmneziaVPN (этап 23.4): сцена «сервер с приложением», импортированный
credential в БД, поиск новых имён.

Новые имена (CredentialSource, JobType.IMPORT_CLIENTS, AuditAction.CLIENTS_IMPORT_*) берутся внутри функций: пока кода
нет, красными становятся тесты, а не вся коллекция."""

import importlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from core.security import encrypt_secret
from infrastructure.vpn.awg_config import generate_secret_keys
from modules.access.models import AccessGrantStatus
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import JobType
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.servers.models.server import VPNServer
from modules.vpn_users.models.user import VPNUser
from tests.fakes.fake_ssh import APP_CLIENTS_TABLE, AmneziaApp, AppPeer, FakeServer
from tests.fixtures import World

CONTAINER = "amnezia-awg2"
PROTOCOL_TYPE = "amnezia_app_awg2"


def new_key() -> str:
    return generate_secret_keys().public_key


def import_job_type() -> JobType:
    return JobType("import_clients")  # JobType.IMPORT_CLIENTS


def import_action(suffix: str) -> AuditAction:
    return AuditAction(f"clients_import_{suffix}")  # CLIENTS_IMPORT_STARTED / COMPLETED / FAILED


def credential_source(value: str) -> Any:
    """Член CredentialSource по значению ("issued" / "imported"). Где лежит enum, не зафиксировано: ищем в обоих
    модулях модели."""
    for module_name in ("modules.access.models", "modules.access.models.vpn_credential"):
        cls = getattr(importlib.import_module(module_name), "CredentialSource", None)
        if cls is not None:
            return cls(value)
    raise AssertionError("CredentialSource is not defined in modules.access.models[.vpn_credential]")


def config_unavailable_error() -> type[Exception]:
    from core import exceptions

    error_class = getattr(exceptions, "ConfigUnavailableError", None)
    assert error_class is not None, "core.exceptions.ConfigUnavailableError is not defined"
    return error_class  # type: ignore[no-any-return]


@dataclass
class Scene:
    """Сервер с установленным приложением AmneziaVPN. Протокола в БД панели ещё нет (его создаст импорт)."""

    server: VPNServer
    fake: FakeServer
    app: AmneziaApp
    keys: list[str]
    names: dict[str, str]
    container: str = CONTAINER

    @property
    def secrets(self) -> list[str]:
        return list(self.app.secrets)


async def make_scene(
    world: World, *, count: int = 3, container: str = CONTAINER, named: bool = True, port: int = 34567
) -> Scene:
    server, _, fake = await world.server(install=False)
    keys = [new_key() for _ in range(count)]
    names = {key: f"Client {index}" for index, key in enumerate(keys)} if named else {}
    app = fake.add_amnezia_app(
        container,
        peers=[AppPeer(key, f"10.8.1.{index + 2}/32") for index, key in enumerate(keys)],
        clients_table=names or None,
        port=port,
    )
    return Scene(server, fake, app, keys, names, container)


def strip_psk(app: AmneziaApp, public_key: str) -> None:
    """Peer без PresharedKey в конфиге приложения (так бывает у клиентов без PSK)."""
    lines = app.files[app.config_path].splitlines()
    out: list[str] = []
    in_target = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[Peer]":
            in_target = False
        if stripped.startswith("PublicKey") and stripped.split("=", 1)[1].strip() == public_key:
            in_target = True
        if in_target and stripped.startswith("PresharedKey"):
            continue
        out.append(line)
    app.files[app.config_path] = "\n".join(out) + "\n"


def set_allowed_ips(app: AmneziaApp, public_key: str, allowed_ips: str) -> None:
    """Подмена AllowedIPs у peer'а в конфиге приложения (как будто его изменили в приложении)."""
    lines = app.files[app.config_path].splitlines()
    out: list[str] = []
    in_target = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[Peer]":
            in_target = False
        if stripped.startswith("PublicKey") and stripped.split("=", 1)[1].strip() == public_key:
            in_target = True
        if in_target and stripped.startswith("AllowedIPs"):
            line = f"AllowedIPs = {allowed_ips}"
        out.append(line)
    app.files[app.config_path] = "\n".join(out) + "\n"


def drop_peer(app: AmneziaApp, public_key: str) -> None:
    """Клиента удалили в приложении: peer и запись в clientsTable исчезли."""
    app.peers = [peer for peer in app.peers if peer.public_key != public_key]
    app.files[app.config_path] = app.render_config()
    table = json.loads(app.files[APP_CLIENTS_TABLE])
    app.files[APP_CLIENTS_TABLE] = json.dumps([entry for entry in table if entry.get("clientId") != public_key])


async def add_app_protocol(world: World, scene: Scene) -> VPNProtocol:
    """Строка протокола приложения в БД (как после импорта)."""
    protocol_type = ProtocolType.AMNEZIA_APP_AWG2 if scene.container == "amnezia-awg2" else ProtocolType.AMNEZIA_APP_AWG
    protocol = VPNProtocol(server_id=scene.server.id, type=protocol_type, port=scene.app.port)
    world.session.add(protocol)
    await world.session.commit()
    return protocol


async def imported_credential(
    world: World,
    protocol: VPNProtocol,
    public_key: str,
    *,
    psk: str | None,
    address: str = "10.8.1.2",
    external_name: str | None = "Phone",
) -> tuple[VPNUser, Subscription, AccessGrant, VPNCredential]:
    """Импортированный credential так, как его оставляет импорт: ACTIVE-подписка и грант, конфига нет, есть PSK."""
    user, subscription, grant = await world.access(protocol)
    subscription.status = AccessGrantStatus.ACTIVE
    grant.status = AccessGrantStatus.ACTIVE
    credential = VPNCredential(
        access_grant_id=grant.id,
        encrypted_config=None,
        client_address=address,
        client_public_key=public_key,
        source=credential_source("imported"),
        encrypted_psk=encrypt_secret(psk) if psk is not None else None,
        external_name=external_name,
    )
    world.session.add(credential)
    await world.session.commit()
    return user, subscription, grant, credential


async def count(world: World, model: Any) -> int:
    return int((await world.session.execute(select(func.count()).select_from(model))).scalar_one())


async def credentials_of(world: World, server_id: UUID) -> list[VPNCredential]:
    return list(
        (
            await world.session.execute(
                select(VPNCredential)
                .execution_options(populate_existing=True)
                .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
                .where(AccessGrant.server_id == server_id)
                .order_by(VPNCredential.created_at)
            )
        ).scalars()
    )
