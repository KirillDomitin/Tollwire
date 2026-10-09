"""Общие фикстуры для тестов с БД (сервисных и интеграционных): сессия, фейковый SSH, сборщик данных World.

Подключаются из корневого conftest через pytest_plugins. Таблицы чистит фикстура clean_db (autouse в подпапках).
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from core.exceptions import NotFoundError
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.schemas import AccessGrantCreate, SubscriptionCreate
from modules.access.service import AccessGrantService, SubscriptionService, VPNCredentialService
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.jobs.models.job import Job, JobType
from modules.protocols.models.protocol import ProtocolType, VPNProtocol
from modules.servers.models.credential import AuthType
from modules.servers.models.server import VPNServer
from modules.servers.schemas import CredentialCreate, ServerCreate
from modules.servers.service import CredentialService, ServerConnectionService, ServerService
from modules.vpn_users.models.user import VPNUser
from modules.vpn_users.schemas import VPNUserCreate
from modules.vpn_users.service import VPNUserService
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as db_session:
        yield db_session


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[UUID, FakeServer]:
    """Подменяет создание SSH-клиента: вместо сети — FakeServer этого сервера. Остальной код — настоящий."""
    servers: dict[UUID, FakeServer] = {}

    async def create_ssh_client(
        self: ServerConnectionService, server_id: UUID, *, private_key: str | None = None
    ) -> FakeSSHClient:
        """private_key — войти ТОЛЬКО этим ключом, а не сохранённой учёткой (так сервис ключа проверяет новый
        ключ). Учётка с auth_type=ssh_key тоже входит по ключу: фейк пускает его, лишь если он в authorized_keys."""
        server = await self.servers.get_by_id(server_id)
        if not server:
            raise NotFoundError
        credential = await self.credentials.get(server_id)  # как настоящий метод: без учётки — CredentialNotFound
        if private_key is None and credential.auth_type == AuthType.SSH_KEY:
            private_key = await self.credentials.get_decrypted_secret(server_id)
        # Сервер, созданный через API, получает свой чистый фейк при первом подключении.
        return FakeSSHClient(servers.setdefault(server_id, FakeServer(host=server.host)), private_key=private_key)

    monkeypatch.setattr(ServerConnectionService, "create_ssh_client", create_ssh_client)
    return servers


class World:
    """Сборщик тестовых данных через настоящие сервисы (не через INSERT в обход правил)."""

    def __init__(self, session: AsyncSession, fakes: dict[UUID, FakeServer]):
        self.session = session
        self.fakes = fakes

    async def server(self, *, install: bool = True) -> tuple[VPNServer, VPNProtocol | None, FakeServer]:
        server = await ServerService(self.session, SYSTEM_ACTOR).create(
            ServerCreate(name=f"srv-{uuid4().hex[:8]}", host="203.0.113.10")
        )
        await CredentialService(self.session, SYSTEM_ACTOR).set_credential(server.id, CredentialCreate(secret="pw"))
        fake = self.fakes[server.id] = FakeServer(host=server.host)
        protocol = None
        if install:
            protocol = await ServerConnectionService(self.session).protocol_install(
                VPNProtocol(server_id=server.id, type=ProtocolType.AMNEZIA_WG)
            )
        return server, protocol, fake

    async def access(
        self, protocol: VPNProtocol, *, starts: timedelta = timedelta(hours=-1), ends: timedelta = timedelta(days=1)
    ) -> tuple[VPNUser, Subscription, AccessGrant]:
        now = datetime.now(timezone.utc)
        user = await VPNUserService(self.session, SYSTEM_ACTOR).create(
            VPNUserCreate(username="user", email=f"{uuid4().hex[:10]}@example.com")
        )
        subscription = await SubscriptionService(self.session, SYSTEM_ACTOR).create(
            user.id, SubscriptionCreate(valid_from=now + starts, valid_until=now + ends)
        )
        grant = await AccessGrantService(self.session, SYSTEM_ACTOR).create(
            subscription.id, AccessGrantCreate(protocol_id=protocol.id)
        )
        return user, subscription, grant

    def credentials(self) -> VPNCredentialService:
        return VPNCredentialService(self.session, SYSTEM_ACTOR)

    async def issued(self, protocol: VPNProtocol) -> tuple[VPNUser, Subscription, AccessGrant, VPNCredential]:
        user, subscription, grant = await self.access(protocol)
        credential = await self.credentials().provision(grant.id)
        return user, subscription, grant, credential

    async def jobs(self, job_type: JobType) -> list[Job]:
        return list((await self.session.execute(select(Job).where(Job.type == job_type))).scalars())

    async def audit(self, action: AuditAction, entity_id: UUID | None = None) -> list[AuditLog]:
        query = select(AuditLog).where(AuditLog.action == action)
        if entity_id:
            query = query.where(AuditLog.entity_id == entity_id)
        return list((await self.session.execute(query)).scalars())


@pytest.fixture
def world(session: AsyncSession, fakes: dict[UUID, FakeServer]) -> World:
    return World(session, fakes)
