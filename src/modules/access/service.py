#!/usr/bin/python
# -*- coding: utf-8 -*-
from base64 import b32encode
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import (
    AlreadyExistsError,
    ConfigUnavailableError,
    InactiveAccessError,
    InvalidDataError,
    LiveCredentialExistsError,
    NotFoundError,
    ReasonConflictError,
    UnsupportedProtocolError,
)
from core.security import decrypt_secret, encrypt_secret
from infrastructure.vpn.provider import VPNProvider
from modules.access.models import AccessGrantStatus, CredentialState
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import AccessGrantRepository, SubscriptionRepository, VPNCredentialRepository
from modules.access.schemas import (
    AccessGrantCreate,
    GatewayDeviceCreate,
    GatewayDeviceGateway,
    GatewayDevicesRead,
    SubscriptionCreate,
    SubscriptionUpdate,
    VPNCredentialConfigRead,
    VPNCredentialRead,
)
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService, diff_changes
from modules.gateway.assignment import GatewayAssignmentService
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.repository import JobRepository
from modules.jobs.service import JobService
from modules.protocols.models.protocol import ProtocolType
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import ServerRepository
from modules.servers.service import ServerConnectionService, provider_class
from modules.vpn_users.repository import VPNUserRepository

_TERMINAL_STATUSES = (AccessGrantStatus.EXPIRED, AccessGrantStatus.REVOKED)

# Имя клиента на сервере. По префиксу sync отличает наших peer'ов от добавленных вручную.
CLIENT_NAME_PREFIX = "grant-"


def client_name(access_grant_id: UUID) -> str:
    return f"{CLIENT_NAME_PREFIX}{access_grant_id.hex[:12]}"


def is_terminal(status: AccessGrantStatus) -> bool:
    return status in _TERMINAL_STATUSES


def inactive_reason(grant: AccessGrant, subscription: Subscription, now: datetime) -> str | None:
    """Почему доступ по гранту сейчас не действует (None — действует).

    Статусы меняет планировщик с задержкой до интервала, поэтому сверяемся и с датами подписки:
    VPN не должен заработать раньше срока и продолжать работать после него.
    """
    if is_terminal(grant.status):
        return f"Access grant is {grant.status.value}"
    if is_terminal(subscription.status):
        return "Subscription is not active"
    if now < subscription.valid_from:
        return "Subscription period has not started yet"
    if now >= subscription.valid_until:
        return "Subscription period has ended"
    return None


@dataclass(frozen=True)
class RenderedConfig:
    """Расшифрованный конфиг клиента и его формат для приложения протокола (None - формата нет)."""

    config: str
    share_link: str | None
    share_qr: list[str] | None


class SubscriptionService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.subscriptions = SubscriptionRepository(session)
        self.grants = AccessGrantRepository(session)
        self.credentials = VPNCredentialRepository(session)
        self.users = VPNUserRepository(session)
        self.audit = AuditService(session, actor)

    async def stage_create(self, user_id: UUID, data: SubscriptionCreate) -> Subscription:
        """Проверки, вставка (flush) и аудит без commit: вызывающий коммитит сам."""
        if not await self.users.get_by_id(user_id):
            raise NotFoundError(f"VPN user '{user_id}' not found")
        if await self.subscriptions.get_active_by_user_id(user_id):
            raise AlreadyExistsError("User already has an active subscription")

        new_subscription = Subscription(user_id=user_id, valid_from=data.valid_from, valid_until=data.valid_until)
        subscription = await self.subscriptions.create(new_subscription)
        self.audit.record(
            AuditAction.SUBSCRIPTION_CREATED,
            AuditEntity.SUBSCRIPTION,
            subscription.id,
            {"user_id": user_id, "valid_from": data.valid_from, "valid_until": data.valid_until},
        )
        return subscription

    async def create(self, user_id: UUID, data: SubscriptionCreate) -> Subscription:
        subscription = await self.stage_create(user_id, data)
        await self.session.commit()
        return subscription

    async def get(self, subscription_id: UUID) -> Subscription:
        subscription = await self.subscriptions.get_by_id(subscription_id)
        if not subscription:
            raise NotFoundError(f"Subscription '{subscription_id}' not found")
        return subscription

    async def list_by_user(self, user_id: UUID) -> Sequence[Subscription]:
        return await self.subscriptions.list_by_user_id(user_id)

    async def update(self, subscription_id: UUID, data: SubscriptionUpdate) -> Subscription:
        subscription = await self.get(subscription_id)
        fields = data.model_dump(exclude_unset=True)
        # PATCH может прислать одну дату — порядок проверяем по итоговым значениям обеих.
        if fields.get("valid_until", subscription.valid_until) <= fields.get("valid_from", subscription.valid_from):
            raise InvalidDataError("valid_until must be after valid_from")
        changes = diff_changes(subscription, fields)
        for field, value in fields.items():
            setattr(subscription, field, value)
        if changes:
            self.audit.record(
                AuditAction.SUBSCRIPTION_UPDATED,
                AuditEntity.SUBSCRIPTION,
                subscription.id,
                {"changes": changes, "user_id": subscription.user_id},
            )
        await self.session.commit()
        return subscription

    async def revoke(self, subscription_id: UUID) -> Subscription:
        """Отзывает подписку и каскадно — все её ещё не завершённые гранты.

        Идемпотентно: повторный вызов не меняет revoked_at. Гранты в терминальном
        статусе (EXPIRED/REVOKED) не трогаются — их история сохраняется как есть.
        """
        subscription = await self.get(subscription_id)
        if subscription.status != AccessGrantStatus.REVOKED:
            now = datetime.now(timezone.utc)
            subscription.status = AccessGrantStatus.REVOKED
            subscription.revoked_at = now
            revoked = []
            for grant in await self.grants.list_by_subscription_id(subscription_id):
                if grant.status not in _TERMINAL_STATUSES:
                    grant.status = AccessGrantStatus.REVOKED
                    grant.revoked_at = now
                    revoked.append(grant)
            self.audit.record(
                AuditAction.SUBSCRIPTION_REVOKED,
                AuditEntity.SUBSCRIPTION,
                subscription.id,
                {"user_id": subscription.user_id, "access_grant_ids": [grant.id for grant in revoked]},
            )
            # Запись на каждый грант — чтобы история гранта (фильтр по entity_id) была полной.
            for grant in revoked:
                self.audit.record(
                    AuditAction.ACCESS_REVOKED,
                    AuditEntity.ACCESS_GRANT,
                    grant.id,
                    {
                        "reason": "subscription_revoked",
                        "subscription_id": subscription.id,
                        "user_id": subscription.user_id,
                        "server_id": grant.server_id,
                    },
                )
            await self.session.commit()
            await VPNCredentialService(self.session, self.actor).schedule_revoke_for_grants(revoked)
        return subscription

    async def delete(self, subscription_id: UUID) -> None:
        subscription = await self.get(subscription_id)
        if await self.credentials.has_live_for_subscription(subscription_id):
            raise LiveCredentialExistsError("Subscription has live VPN clients, revoke it first")
        await self.subscriptions.delete(subscription)
        self.audit.record(
            AuditAction.SUBSCRIPTION_DELETED,
            AuditEntity.SUBSCRIPTION,
            subscription.id,
            {"user_id": subscription.user_id},
        )
        await self.session.commit()

    async def list_due_for_expiry(self) -> Sequence[UUID]:
        return await self.subscriptions.list_due_for_expiry(datetime.now(timezone.utc))

    async def list_due_for_activation(self) -> Sequence[UUID]:
        return await self.subscriptions.list_due_for_activation(datetime.now(timezone.utc))

    async def activate(self, subscription_id: UUID) -> Subscription:
        """PENDING → ACTIVE, когда начался период (valid_from <= сейчас < valid_until).

        Идемпотентно: не-PENDING подписку и подписку вне периода не трогает. Гранты не меняются —
        грант становится ACTIVE, когда для него реально создан клиент на сервере (provision).
        """
        subscription = await self.get(subscription_id)
        now = datetime.now(timezone.utc)
        if subscription.status != AccessGrantStatus.PENDING:
            return subscription
        if not subscription.valid_from <= now < subscription.valid_until:
            return subscription
        subscription.status = AccessGrantStatus.ACTIVE
        self.audit.record(
            AuditAction.SUBSCRIPTION_ACTIVATED,
            AuditEntity.SUBSCRIPTION,
            subscription.id,
            {"user_id": subscription.user_id, "valid_from": subscription.valid_from},
        )
        await self.session.commit()
        return subscription

    async def expire(self, subscription_id: UUID) -> Subscription:
        """Переводит подписку и её гранты в EXPIRED, если valid_until уже наступил.

        Идемпотентно: подписку в терминальном статусе (EXPIRED/REVOKED) не трогает.
        REVOKED имеет приоритет над истечением по времени — явный отзыв не
        переписывается автоматическим переходом.
        """
        subscription = await self.get(subscription_id)
        if subscription.status in _TERMINAL_STATUSES:
            return subscription
        if datetime.now(timezone.utc) < subscription.valid_until:
            return subscription

        subscription.status = AccessGrantStatus.EXPIRED
        expired = []
        for grant in await self.grants.list_by_subscription_id(subscription_id):
            if grant.status not in _TERMINAL_STATUSES:
                grant.status = AccessGrantStatus.EXPIRED
                expired.append(grant)
        self.audit.record(
            AuditAction.SUBSCRIPTION_EXPIRED,
            AuditEntity.SUBSCRIPTION,
            subscription.id,
            {"user_id": subscription.user_id, "valid_until": subscription.valid_until},
        )
        for grant in expired:
            self.audit.record(
                AuditAction.ACCESS_EXPIRED,
                AuditEntity.ACCESS_GRANT,
                grant.id,
                {"subscription_id": subscription.id, "user_id": subscription.user_id, "server_id": grant.server_id},
            )
        await self.session.commit()
        await VPNCredentialService(self.session, self.actor).schedule_revoke_for_grants(expired)
        return subscription


class AccessGrantService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.grants = AccessGrantRepository(session)
        self.subscriptions = SubscriptionRepository(session)
        self.credentials = VPNCredentialRepository(session)
        self.servers = ServerRepository(session)
        self.protocols = ProtocolRepository(session)
        self.audit = AuditService(session, actor)

    async def stage_create(self, subscription_id: UUID, data: AccessGrantCreate) -> AccessGrant:
        """Проверки, вставка (flush) и аудит без commit: вызывающий коммитит сам."""
        subscription = await self.subscriptions.get_by_id(subscription_id)
        if not subscription:
            raise NotFoundError(f"Subscription '{subscription_id}' not found")

        protocol = await self.protocols.get_by_id(data.protocol_id)
        if not protocol:
            raise NotFoundError(f"Protocol '{data.protocol_id}' not found or not available")

        conflict = await self.grants.find_active_conflict(subscription.user_id, protocol.server_id, protocol.id)
        if conflict:
            raise AlreadyExistsError("An active access grant for this server/protocol already exists")

        new_grant = AccessGrant(
            subscription_id=subscription_id,
            server_id=protocol.server_id,
            protocol_id=protocol.id,
        )
        grant = await self.grants.create(new_grant)
        self.audit.record(
            AuditAction.ACCESS_GRANTED,
            AuditEntity.ACCESS_GRANT,
            grant.id,
            {
                "user_id": subscription.user_id,
                "subscription_id": subscription_id,
                "server_id": protocol.server_id,
                "protocol_id": protocol.id,
            },
        )
        return grant

    async def create(self, subscription_id: UUID, data: AccessGrantCreate) -> AccessGrant:
        grant = await self.stage_create(subscription_id, data)
        await self.session.commit()
        return grant

    async def get(self, access_grant_id: UUID) -> AccessGrant:
        grant = await self.grants.get_by_id(access_grant_id)
        if not grant:
            raise NotFoundError(f"Access grant '{access_grant_id}' not found")
        return grant

    async def list_by_subscription(self, subscription_id: UUID) -> Sequence[AccessGrant]:
        return await self.grants.list_by_subscription_id(subscription_id)

    # Статус гранта нельзя выставить напрямую: только по правилам — /revoke (с отзывом клиента на сервере),
    # планировщик (истечение/активация) и выдача клиента (ACTIVE). Отозванный доступ не «оживает»: нужен новый грант.

    async def revoke(self, access_grant_id: UUID) -> AccessGrant:
        grant = await self.get(access_grant_id)
        if grant.status != AccessGrantStatus.REVOKED:
            grant.status = AccessGrantStatus.REVOKED
            grant.revoked_at = datetime.now(timezone.utc)
            subscription = await self.subscriptions.get_by_id(grant.subscription_id)
            self.audit.record(
                AuditAction.ACCESS_REVOKED,
                AuditEntity.ACCESS_GRANT,
                grant.id,
                {
                    "reason": "manual",
                    "subscription_id": grant.subscription_id,
                    "user_id": subscription.user_id if subscription else None,
                    "server_id": grant.server_id,
                },
            )
            await self.session.commit()
            await VPNCredentialService(self.session, self.actor).schedule_revoke_for_grants([grant])
        return grant

    async def delete(self, access_grant_id: UUID) -> None:
        grant = await self.get(access_grant_id)
        if await self.credentials.get_live_by_access_grant_id(access_grant_id):
            raise LiveCredentialExistsError("Access grant has a live VPN client, revoke it first")
        await self.grants.delete(grant)
        self.audit.record(
            AuditAction.ACCESS_DELETED,
            AuditEntity.ACCESS_GRANT,
            grant.id,
            {"subscription_id": grant.subscription_id, "server_id": grant.server_id},
        )
        await self.session.commit()


class VPNCredentialService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.credentials = VPNCredentialRepository(session)
        self.grants = AccessGrantRepository(session)
        self.subscriptions = SubscriptionRepository(session)
        self.protocols = ProtocolRepository(session)
        self.jobs = JobService(session, actor)
        self.job_repository = JobRepository(session)
        self.connections = ServerConnectionService(session)
        self.audit = AuditService(session, actor)
        self.actor = actor

    async def stage_issue(
        self, access_grant_id: UUID, device_name: str | None = None, request_id: UUID | None = None
    ) -> tuple[VPNCredential, Job]:
        """Reserve capacity and its targeted job in one transaction, before any SSH."""
        grant = await self._ensure_grant_is_live(access_grant_id)
        subscription = await self.subscriptions.get_by_id(grant.subscription_id)
        assert subscription is not None
        await self.credentials.lock_user(subscription.user_id)
        await self.session.refresh(grant)
        await self.session.refresh(subscription)
        grant = await self._ensure_grant_is_live(access_grant_id)
        protocol = await self.protocols.get_by_id(grant.protocol_id)
        if protocol is None:
            raise NotFoundError("Protocol not found")
        if request_id is not None:
            previous = await self.credentials.by_request_id(request_id)
            if previous is not None:
                if previous.access_grant_id != grant.id:
                    raise NotFoundError("Device not found")
                job = (await self.session.execute(select(Job).where(
                    Job.type.in_((JobType.CREATE_DEVICE, JobType.CREATE_USER)),
                    Job.params["credential_id"].astext == str(previous.id),
                ).order_by(Job.created_at.desc()).limit(1))).scalar_one_or_none()
                if job is None:
                    raise AlreadyExistsError("Device request has already completed")
                return previous, job
        gateway = protocol.type == ProtocolType.AMNEZIA_GATEWAY
        if gateway:
            if await self.credentials.count_gateway_for_user(subscription.user_id) >= 5:
                raise ReasonConflictError("device_limit")
        else:
            existing = await self.credentials.live_for_grant(grant.id)
            if existing:
                if any(item.state in (CredentialState.RESERVED, CredentialState.PROVISIONING) for item in existing):
                    raise AlreadyExistsError("Credential is already being issued")
                raise AlreadyExistsError("Access grant already has a live credential")
        credential_id = uuid4()
        credential = VPNCredential(
            id=credential_id, access_grant_id=grant.id, state=CredentialState.RESERVED,
            is_gateway=gateway, device_name=device_name, request_id=request_id,
            peer_name=("dev-" + b32encode(credential_id.bytes).decode().rstrip("=").lower()
                       if gateway else client_name(grant.id)),
            encrypted_config=None, client_address=None, client_public_key=None,
        )
        await self.credentials.create(credential)
        job = Job(type=JobType.CREATE_DEVICE if gateway else JobType.CREATE_USER,
                  server_id=grant.server_id, access_grant_id=grant.id,
                  admin_id=self.actor.admin_id, params={"credential_id": str(credential.id)})
        self.session.add(job)
        await self.session.flush()
        return credential, job

    async def schedule_issue(
        self, access_grant_id: UUID, device_name: str | None = None, request_id: UUID | None = None
    ) -> Job:
        _, job = await self.stage_issue(access_grant_id, device_name, request_id)
        await self.session.commit()
        await self.jobs.job_queue.enqueue(job.id)
        return job

    async def provision(self, access_grant_id: UUID, credential_id: UUID | None = None) -> VPNCredential:
        # Legacy CREATE_USER targets a grant. New jobs always target the reserved credential.
        if credential_id is None:
            await self._ensure_grant_is_live(access_grant_id)
            live = await self.credentials.live_for_grant(access_grant_id)
            if live:
                credential_id = live[0].id
            else:
                reserved, staged_job = await self.stage_issue(access_grant_id)
                await self.session.delete(staged_job)  # legacy job already owns this operation
                credential_id = reserved.id
                await self.session.commit()
        credential = await self.session.get(
            VPNCredential, credential_id, with_for_update=True, populate_existing=True
        )
        if credential is None:
            raise NotFoundError("Device not found")
        if credential.access_grant_id != access_grant_id:
            raise NotFoundError("Device not found")
        if credential.revoked_at is not None or credential.state == CredentialState.REVOKING:
            return credential
        try:
            grant = await self._ensure_grant_is_live(access_grant_id)
        except InactiveAccessError:
            await self.deprovision(access_grant_id, credential.id)
            raise
        if credential.state == CredentialState.ACTIVE:
            await self._attach_to_gateway(credential, grant.protocol_id)
            return credential
        protocol = await self.protocols.get_by_id(grant.protocol_id)
        if protocol is None:
            raise NotFoundError("Protocol not found")
        recovering = credential.state == CredentialState.PROVISIONING
        credential.state = CredentialState.PROVISIONING
        await self.session.commit()  # durable intent: failure or worker death must retain the slot
        ssh_client = await self.connections.create_ssh_client(grant.server_id)
        provider = self.connections.build_provider(ssh_client, protocol.type)
        async with ssh_client:
            name = credential.peer_name or client_name(grant.id)
            if recovering:
                await self._cleanup_peer(provider, credential, name)
            client = await provider.create_client(name)
            try:
                grant = await self._ensure_grant_is_live(access_grant_id, lock=True)
                refreshed = await self.session.get(VPNCredential, credential_id, populate_existing=True,
                                                    with_for_update=True)
                assert refreshed is not None
                credential = refreshed
                if credential.state == CredentialState.REVOKING or credential.revoked_at is not None:
                    raise InactiveAccessError("Device is revoked")
            except InactiveAccessError:
                await provider.revoke_client(client.public_key)
                credential.state = CredentialState.REVOKED
                credential.revoked_at = datetime.now(timezone.utc)
                await self.session.commit()
                raise
        credential.encrypted_config = encrypt_secret(client.config)
        credential.client_address = client.address
        credential.client_public_key = client.public_key
        credential.state = CredentialState.ACTIVE
        grant.status = AccessGrantStatus.ACTIVE
        self.audit.record(AuditAction.CREDENTIAL_ISSUED, AuditEntity.VPN_CREDENTIAL, credential.id,
                          {"access_grant_id": grant.id, "server_id": grant.server_id,
                           "address": client.address, "public_key": client.public_key})
        await self.session.commit()
        await self._attach_to_gateway(credential, grant.protocol_id)
        return credential

    async def _cleanup_peer(self, provider: VPNProvider, credential: VPNCredential, name: str) -> None:
        # A timed out create may have persisted a peer whose key never reached the database.
        await provider.ensure_client_removed(name=name, public_key=credential.client_public_key)

    async def _attach_to_gateway(self, credential: VPNCredential, protocol_id: UUID) -> None:
        protocol = await self.protocols.get_by_id(protocol_id)
        if protocol is not None and protocol.type == ProtocolType.AMNEZIA_GATEWAY:
            await GatewayAssignmentService(self.session, self.actor).attach(credential)

    async def deprovision(
        self, access_grant_id: UUID, credential_id: UUID | None = None
    ) -> VPNCredential | None:
        grant = await self.grants.get_by_id(access_grant_id)
        if grant is None:
            raise NotFoundError("Access grant not found")
        if credential_id is None:
            result = None
            for credential in await self.credentials.live_for_grant(grant.id):
                result = await self.deprovision(grant.id, credential.id)
            return result
        credential = await self.get(credential_id)
        if credential.access_grant_id != grant.id:
            raise NotFoundError("Device not found")
        if credential.revoked_at is not None:
            return credential
        was_reserved = credential.state == CredentialState.RESERVED
        credential.state = CredentialState.REVOKING
        await self.session.commit()
        protocol = await self.protocols.get_by_id(grant.protocol_id)
        if protocol is None:
            raise NotFoundError("Protocol not found")
        if not was_reserved:
            ssh_client = await self.connections.create_ssh_client(grant.server_id)
            provider = self.connections.build_provider(ssh_client, protocol.type)
            async with ssh_client:
                await self._cleanup_peer(provider, credential, credential.peer_name or client_name(grant.id))
        credential.revoked_at = datetime.now(timezone.utc)
        credential.state = CredentialState.REVOKED
        self.audit.record(AuditAction.CREDENTIAL_REVOKED, AuditEntity.VPN_CREDENTIAL, credential.id,
                          {"access_grant_id": grant.id, "server_id": grant.server_id,
                           "public_key": credential.client_public_key})
        await self.session.commit()
        if protocol.type == ProtocolType.AMNEZIA_GATEWAY:
            await GatewayAssignmentService(self.session, self.actor).detach(credential)
        return credential

    async def schedule_revoke_for_grants(self, grants: Sequence[AccessGrant]) -> list[Job]:
        jobs = []
        for grant in grants:
            credentials = await self.credentials.live_for_grant(grant.id)
            for credential in credentials:
                result = await self.schedule_revoke(credential.id)
                if isinstance(result, Job):
                    jobs.append(result)
            if not credentials and await self.job_repository.has_unfinished_for_grant(JobType.CREATE_USER, grant.id):
                jobs.append(await self.jobs.create(JobType.REVOKE_ACCESS, server_id=grant.server_id,
                                                   access_grant_id=grant.id))
        return jobs

    async def get(self, credential_id: UUID) -> VPNCredential:
        credential = await self.credentials.get_by_id(credential_id)
        if not credential:
            raise NotFoundError(f"Credential '{credential_id}' not found")
        return credential

    async def list_by_access_grant(self, access_grant_id: UUID) -> Sequence[VPNCredential]:
        return await self.credentials.list_by_access_grant_id(access_grant_id)

    async def reveal_config(self, credential_id: UUID) -> VPNCredentialConfigRead:
        """Выдаёт расшифрованный конфиг админу. Сам конфиг не логировать — в журнал идёт только факт выдачи.

        У импортированного клиента конфига нет: ConfigUnavailableError (409), аудита не пишется."""
        credential = await self.get(credential_id)
        if credential.revoked_at is not None or credential.state == CredentialState.REVOKING:
            raise InactiveAccessError("Credential is revoked")
        grant = await self._ensure_grant_is_live(credential.access_grant_id)
        if credential.encrypted_config is None:
            # До записи в аудит: ничего не показали, значит и «просмотра» не было.
            raise ConfigUnavailableError(
                "Config is not stored: the client was imported from AmneziaVPN, its private key stays in the app"
            )
        self.audit.record(
            AuditAction.CONFIG_VIEWED,
            AuditEntity.VPN_CREDENTIAL,
            credential.id,
            {"access_grant_id": credential.access_grant_id, "server_id": grant.server_id},
        )
        # Сначала фиксируем факт выдачи: если запись не сохранится, конфиг не уйдёт.
        await self.session.commit()
        rendered = await self.render_config(credential, grant)
        return VPNCredentialConfigRead(
            id=credential.id,
            access_grant_id=credential.access_grant_id,
            config=rendered.config,
            share_link=rendered.share_link,
            share_qr=rendered.share_qr,
        )

    async def render_config(self, credential: VPNCredential, grant: AccessGrant) -> RenderedConfig:
        """Расшифровывает конфиг и строит форматы приложения. Единственное место расшифровки.

        Не проверяет доступ и не пишет аудит: это дело вызывающего (reveal_config для админа - с аудитом; страница
        конфига по ссылке - после своей проверки ссылки). У импортированного клиента конфига нет."""
        if credential.encrypted_config is None:
            raise ConfigUnavailableError(
                "Config is not stored: the client was imported from AmneziaVPN, its private key stays in the app"
            )
        config = decrypt_secret(credential.encrypted_config)
        share_link, share_qr = await self._app_formats(grant, config)
        return RenderedConfig(config=config, share_link=share_link, share_qr=share_qr)

    async def _app_formats(self, grant: AccessGrant, config: str) -> tuple[str | None, list[str] | None]:
        """Конфиг в формате приложения протокола (строка и серия QR) с именем сервера из панели.
        Не вышло — клиенту остаётся сам конфиг."""
        protocol = await self.protocols.get_by_id(grant.protocol_id)
        server = await ServerRepository(self.session).get_by_id(grant.server_id)
        if protocol is None or server is None:
            return None, None
        try:
            provider = provider_class(protocol.type)
            return provider.share_link(config, server.name), provider.share_qr_codes(config, server.name)
        except (ValueError, UnsupportedProtocolError):
            return None, None

    async def schedule_revoke(self, credential_id: UUID) -> VPNCredential | Job:
        credential = await self.get(credential_id)
        if credential.revoked_at is not None:
            return credential
        grant = await self.grants.get_by_id(credential.access_grant_id)
        if grant is None:
            raise NotFoundError("Access grant not found")
        # Serialize request/revoke against completion of an in-flight create.
        locked = await self.session.get(VPNCredential, credential_id, with_for_update=True,
                                        populate_existing=True)
        assert locked is not None
        credential = locked
        if credential.revoked_at is not None:
            await self.session.commit()
            return credential
        credential.state = CredentialState.REVOKING
        existing = (await self.session.execute(select(Job).where(
            Job.type.in_((JobType.REVOKE_DEVICE, JobType.REVOKE_ACCESS)),
            Job.params["credential_id"].astext == str(credential.id),
            Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
        ).limit(1))).scalar_one_or_none()
        if existing is not None:
            await self.session.commit()
            return existing
        return await self.jobs.create(JobType.REVOKE_DEVICE if credential.is_gateway else JobType.REVOKE_ACCESS,
                                      server_id=grant.server_id,
                                      access_grant_id=grant.id, params={"credential_id": str(credential.id)})

    async def gateway_devices(self, user_id: UUID) -> GatewayDevicesRead:
        if await VPNUserRepository(self.session).get_by_id(user_id) is None:
            raise NotFoundError("User not found")
        devices: list[VPNCredentialRead] = []
        gateways: list[GatewayDeviceGateway] = []
        for subscription in await self.subscriptions.list_by_user_id(user_id):
            for grant in await self.grants.list_by_subscription_id(subscription.id):
                protocol = await self.protocols.get_by_id(grant.protocol_id)
                if protocol is None or protocol.type != ProtocolType.AMNEZIA_GATEWAY:
                    continue
                devices.extend(VPNCredentialRead.model_validate(item)
                               for item in await self.credentials.live_for_grant(grant.id))
                if inactive_reason(grant, subscription, datetime.now(timezone.utc)) is None:
                    server = await ServerRepository(self.session).get_by_id(grant.server_id)
                    if server is not None:
                        gateways.append(GatewayDeviceGateway(grant_id=grant.id, protocol_id=protocol.id,
                            server_id=server.id, server_name=server.name))
        return GatewayDevicesRead(occupied=len(devices), devices=devices, gateways=gateways)

    async def add_gateway_device(self, user_id: UUID, data: GatewayDeviceCreate) -> Job:
        grant = await self.grants.get_by_id(data.grant_id)
        subscription = await self.subscriptions.get_by_id(grant.subscription_id) if grant else None
        protocol = await self.protocols.get_by_id(grant.protocol_id) if grant else None
        if subscription is None or subscription.user_id != user_id or protocol is None or (
            protocol.type != ProtocolType.AMNEZIA_GATEWAY
        ):
            raise NotFoundError("Gateway access not found")
        return await self.schedule_issue(data.grant_id, data.device_name, data.request_id)

    async def _ensure_grant_is_live(self, access_grant_id: UUID, *, lock: bool = False) -> AccessGrant:
        # Проверяем и грант, и подписку: отзыв/истечение подписки каскадят на гранты,
        # но проверка подписки страхует от рассинхрона (данные до каскада, правки через PATCH подписки).
        grant = await self.grants.get_by_id(access_grant_id, lock=lock)
        if not grant:
            raise NotFoundError(f"Access grant '{access_grant_id}' not found")
        subscription = await self.subscriptions.get_by_id(grant.subscription_id, lock=lock)
        if not subscription:
            raise InactiveAccessError("Subscription is not active")
        reason = inactive_reason(grant, subscription, datetime.now(timezone.utc))
        if reason:
            raise InactiveAccessError(reason)
        return grant
