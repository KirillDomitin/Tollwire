#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AlreadyExistsError, InactiveAccessError, LiveCredentialExistsError, NotFoundError
from core.security import decrypt_secret, encrypt_secret
from modules.access.models import AccessGrantStatus
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import AccessGrantRepository, SubscriptionRepository, VPNCredentialRepository
from modules.access.schemas import (
    AccessGrantCreate,
    AccessGrantUpdate,
    SubscriptionCreate,
    SubscriptionUpdate,
    VPNCredentialConfigRead,
)
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService, diff_changes
from modules.jobs.models.job import Job, JobType
from modules.jobs.repository import JobRepository
from modules.jobs.service import JobService
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import ServerRepository
from modules.servers.service import ServerConnectionService
from modules.vpn_users.repository import VPNUserRepository

_TERMINAL_STATUSES = (AccessGrantStatus.EXPIRED, AccessGrantStatus.REVOKED)

# Имя клиента на сервере. По префиксу sync отличает наших peer'ов от добавленных вручную.
CLIENT_NAME_PREFIX = "grant-"


def client_name(access_grant_id: UUID) -> str:
    return f"{CLIENT_NAME_PREFIX}{access_grant_id.hex[:12]}"


def is_terminal(status: AccessGrantStatus) -> bool:
    return status in _TERMINAL_STATUSES


class SubscriptionService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.subscriptions = SubscriptionRepository(session)
        self.grants = AccessGrantRepository(session)
        self.credentials = VPNCredentialRepository(session)
        self.users = VPNUserRepository(session)
        self.audit = AuditService(session, actor)

    async def create(self, user_id: UUID, data: SubscriptionCreate) -> Subscription:
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
        changes = diff_changes(subscription, fields)
        for field, value in fields.items():
            setattr(subscription, field, value)
        if changes:
            self.audit.record(
                AuditAction.SUBSCRIPTION_UPDATED, AuditEntity.SUBSCRIPTION, subscription.id, {"changes": changes}
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
                    {"reason": "subscription_revoked", "subscription_id": subscription.id},
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
                AuditAction.ACCESS_EXPIRED, AuditEntity.ACCESS_GRANT, grant.id, {"subscription_id": subscription.id}
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

    async def create(self, subscription_id: UUID, data: AccessGrantCreate) -> AccessGrant:
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
        await self.session.commit()
        return grant

    async def get(self, access_grant_id: UUID) -> AccessGrant:
        grant = await self.grants.get_by_id(access_grant_id)
        if not grant:
            raise NotFoundError(f"Access grant '{access_grant_id}' not found")
        return grant

    async def list_by_subscription(self, subscription_id: UUID) -> Sequence[AccessGrant]:
        return await self.grants.list_by_subscription_id(subscription_id)

    async def update(self, access_grant_id: UUID, data: AccessGrantUpdate) -> AccessGrant:
        grant = await self.get(access_grant_id)
        fields = data.model_dump(exclude_unset=True)
        changes = diff_changes(grant, fields)
        for field, value in fields.items():
            setattr(grant, field, value)
        if changes:
            self.audit.record(AuditAction.ACCESS_UPDATED, AuditEntity.ACCESS_GRANT, grant.id, {"changes": changes})
        await self.session.commit()
        return grant

    async def revoke(self, access_grant_id: UUID) -> AccessGrant:
        grant = await self.get(access_grant_id)
        if grant.status != AccessGrantStatus.REVOKED:
            grant.status = AccessGrantStatus.REVOKED
            grant.revoked_at = datetime.now(timezone.utc)
            self.audit.record(AuditAction.ACCESS_REVOKED, AuditEntity.ACCESS_GRANT, grant.id, {"reason": "manual"})
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

    async def schedule_issue(self, access_grant_id: UUID) -> Job:
        """Ставит задачу CREATE_USER: воркер создаст клиента на сервере и сохранит credential.

        Быстрые проверки делаем здесь, чтобы сразу ответить 404/409, а не получить
        упавшую задачу. Воркер всё равно перепроверит — за время в очереди всё могло измениться.
        """
        grant = await self._ensure_grant_is_live(access_grant_id)
        if await self.credentials.get_live_by_access_grant_id(access_grant_id):
            raise AlreadyExistsError("Access grant already has a live credential")
        if await self.job_repository.has_unfinished_for_grant(JobType.CREATE_USER, access_grant_id):
            raise AlreadyExistsError("Credential is already being issued")
        return await self.jobs.create(JobType.CREATE_USER, server_id=grant.server_id, access_grant_id=grant.id)

    async def provision(self, access_grant_id: UUID) -> VPNCredential:
        """Создаёт клиента на сервере и сохраняет его конфиг в зашифрованном виде.

        Вызывается воркером под ServerLock сервера гранта. Идемпотентно: если живой
        credential уже есть (повтор задачи), возвращает его, второго клиента не создаёт.
        """
        grant = await self._ensure_grant_is_live(access_grant_id)
        live = await self.credentials.get_live_by_access_grant_id(access_grant_id)
        if live:
            return live
        protocol = await self.protocols.get_by_id(grant.protocol_id)
        if not protocol:
            raise NotFoundError(f"Protocol '{grant.protocol_id}' not found")

        ssh_client = await self.connections.create_ssh_client(grant.server_id)
        provider = self.connections.build_provider(ssh_client, protocol.type)
        async with ssh_client:
            client = await provider.create_client(client_name(grant.id))
        # Если коммит ниже упадёт, на сервере останется peer без записи в БД — его подберёт sync (блок 13).
        credential = VPNCredential(
            access_grant_id=grant.id,
            encrypted_config=encrypt_secret(client.config),
            client_address=client.address,
            client_public_key=client.public_key,
        )
        await self.credentials.create(credential)
        grant.status = AccessGrantStatus.ACTIVE
        self.audit.record(
            AuditAction.CREDENTIAL_ISSUED,
            AuditEntity.VPN_CREDENTIAL,
            credential.id,
            {
                "access_grant_id": grant.id,
                "server_id": grant.server_id,
                "address": client.address,
                "public_key": client.public_key,
            },
        )
        await self.session.commit()
        return credential

    async def deprovision(self, access_grant_id: UUID) -> VPNCredential | None:
        """Удаляет клиента гранта с сервера и помечает credential отозванным.

        Вызывается воркером под ServerLock. Идемпотентно: нет живого credential — ничего не делает.
        Состояние гранта не проверяем: отзывать нужно как раз у отозванных/истёкших.
        """
        grant = await self.grants.get_by_id(access_grant_id)
        if not grant:
            raise NotFoundError(f"Access grant '{access_grant_id}' not found")
        credential = await self.credentials.get_live_by_access_grant_id(access_grant_id)
        if not credential:
            return None
        protocol = await self.protocols.get_by_id(grant.protocol_id)
        if not protocol:
            raise NotFoundError(f"Protocol '{grant.protocol_id}' not found")

        ssh_client = await self.connections.create_ssh_client(grant.server_id)
        provider = self.connections.build_provider(ssh_client, protocol.type)
        async with ssh_client:
            await provider.revoke_client(credential.client_public_key)
        credential.revoked_at = datetime.now(timezone.utc)
        self.audit.record(
            AuditAction.CREDENTIAL_REVOKED,
            AuditEntity.VPN_CREDENTIAL,
            credential.id,
            {"access_grant_id": grant.id, "server_id": grant.server_id, "public_key": credential.client_public_key},
        )
        await self.session.commit()
        return credential

    async def schedule_revoke_for_grants(self, grants: Sequence[AccessGrant]) -> list[Job]:
        """Ставит REVOKE_ACCESS тем грантам, у которых на сервере есть (или вот-вот появится) клиент.

        «Вот-вот появится» — незавершённая CREATE_USER. Порядок выполнения не важен: если она
        успеет первой, REVOKE_ACCESS увидит свежий credential и уберёт его; если опоздает —
        provision откажет сам, потому что грант уже не живой.
        """
        jobs = []
        for grant in grants:
            if await self.credentials.get_live_by_access_grant_id(
                grant.id
            ) or await self.job_repository.has_unfinished_for_grant(JobType.CREATE_USER, grant.id):
                jobs.append(
                    await self.jobs.create(JobType.REVOKE_ACCESS, server_id=grant.server_id, access_grant_id=grant.id)
                )
        return jobs

    async def get(self, credential_id: UUID) -> VPNCredential:
        credential = await self.credentials.get_by_id(credential_id)
        if not credential:
            raise NotFoundError(f"Credential '{credential_id}' not found")
        return credential

    async def list_by_access_grant(self, access_grant_id: UUID) -> Sequence[VPNCredential]:
        return await self.credentials.list_by_access_grant_id(access_grant_id)

    async def reveal_config(self, credential_id: UUID) -> VPNCredentialConfigRead:
        """Выдаёт расшифрованный конфиг админу. Сам конфиг не логировать — в журнал идёт только факт выдачи."""
        credential = await self.get(credential_id)
        if credential.revoked_at is not None:
            raise InactiveAccessError("Credential is revoked")
        await self._ensure_grant_is_live(credential.access_grant_id)
        self.audit.record(
            AuditAction.CONFIG_VIEWED,
            AuditEntity.VPN_CREDENTIAL,
            credential.id,
            {"access_grant_id": credential.access_grant_id},
        )
        # Сначала фиксируем факт выдачи: если запись не сохранится, конфиг не уйдёт.
        await self.session.commit()
        return VPNCredentialConfigRead(
            id=credential.id,
            access_grant_id=credential.access_grant_id,
            config=decrypt_secret(credential.encrypted_config),
        )

    async def schedule_revoke(self, credential_id: UUID) -> VPNCredential | Job:
        """Уже отозванный credential возвращается как есть (идемпотентно), иначе ставится REVOKE_ACCESS.

        Грант при этом остаётся живым: по нему можно выпустить новый credential (ротация ключей).
        """
        credential = await self.get(credential_id)
        if credential.revoked_at is not None:
            return credential
        grant = await self.grants.get_by_id(credential.access_grant_id)
        if not grant:
            raise NotFoundError(f"Access grant '{credential.access_grant_id}' not found")
        return await self.jobs.create(JobType.REVOKE_ACCESS, server_id=grant.server_id, access_grant_id=grant.id)

    async def _ensure_grant_is_live(self, access_grant_id: UUID) -> AccessGrant:
        # Проверяем и грант, и подписку: отзыв/истечение подписки каскадят на гранты,
        # но проверка подписки страхует от рассинхрона (данные до каскада, ручные правки через PATCH).
        grant = await self.grants.get_by_id(access_grant_id)
        if not grant:
            raise NotFoundError(f"Access grant '{access_grant_id}' not found")
        if grant.status in _TERMINAL_STATUSES:
            raise InactiveAccessError(f"Access grant is {grant.status.value}")
        subscription = await self.subscriptions.get_by_id(grant.subscription_id)
        if not subscription or subscription.status in _TERMINAL_STATUSES:
            raise InactiveAccessError("Subscription is not active")
        return grant
