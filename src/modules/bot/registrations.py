#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import NoReturn
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AppError, NotFoundError, RegistrationConflictError, StateConflictError
from core.security import hash_token
from modules.access.repository import AccessGrantRepository, SubscriptionRepository, VPNCredentialRepository
from modules.access.schemas import AccessGrantCreate, SubscriptionCreate
from modules.access.service import AccessGrantService, SubscriptionService, VPNCredentialService
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.bot.invites import InviteStatus, invite_status
from modules.bot.models.registration import BotRegistration, RegistrationStatus
from modules.bot.payment import InviteFree, PaymentPolicy, PaymentStatus
from modules.bot.repository import BotInviteRepository, BotRegistrationRepository
from modules.bot.schemas import RegistrationCreate, UserStatusRead
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.jobs.repository import JobRepository
from modules.protocols.repository import ProtocolRepository
from modules.vpn_users.models.user import VPNUserStatus
from modules.vpn_users.repository import VPNUserRepository
from modules.vpn_users.schemas import VPNUserCreate
from modules.vpn_users.service import VPNUserService

logger = logging.getLogger("bot")

# Причины отказа в регистрации (поле reason ответа 409): по ним бот говорит человеку, что не так.
INVITE_INVALID = "invite_invalid"  # нет такого, отозвано или протокол приглашения удалён
INVITE_EXPIRED = "invite_expired"
INVITE_USED = "invite_used"
EMAIL_TAKEN = "email_taken"
TELEGRAM_TAKEN = "telegram_taken"

_INTERRUPTED = "Provisioning was interrupted"
_ERROR_MAX_LENGTH = 1000


class BotRegistrationService:
    def __init__(self, session: AsyncSession, actor: AuditActor, payment: PaymentPolicy | None = None):
        self.session = session
        self.actor = actor
        self.payment: PaymentPolicy = payment or InviteFree()
        self.registrations = BotRegistrationRepository(session)
        self.invites = BotInviteRepository(session)
        self.users = VPNUserRepository(session)
        self.audit = AuditService(session, actor)

    # --- регистрация -----------------------------------------------------------------------------------------------

    async def register(self, data: RegistrationCreate, bot_key_id: UUID | None = None) -> BotRegistration:
        """Занимает приглашение и запускает выдачу доступа. Повтор тем же человеком возвращает ту же регистрацию.

        Пользователь, подписка и грант создаются одной транзакцией вместе с задачей CREATE_USER (только БД, SSH
        делает воркер). Уникальные индексы ловят то, что проверки пропустили при гонке: при IntegrityError повторяем
        с чистого листа, и проверки называют настоящую причину отказа.
        """
        try:
            return await self._register(data, bot_key_id)
        except IntegrityError:
            await self.session.rollback()
        try:
            return await self._register(data, bot_key_id)
        except IntegrityError:
            await self.session.rollback()
            raise RegistrationConflictError("conflict") from None

    async def _register(self, data: RegistrationCreate, bot_key_id: UUID | None) -> BotRegistration:
        now = datetime.now(timezone.utc)
        invite = await self.invites.get_by_hash(hash_token(data.code), lock=True)
        if invite is None:
            await self._refuse(INVITE_INVALID)
        state = invite_status(invite, now)
        if state is InviteStatus.USED:
            existing = await self.registrations.get_by_invite_id(invite.id)
            if existing is not None and existing.telegram_id == data.telegram_id:
                await self.session.commit()  # снять блокировку: ничего не менялось
                return existing
            await self._refuse(INVITE_USED)
        if state is InviteStatus.EXPIRED:
            await self._refuse(INVITE_EXPIRED)
        if state is not InviteStatus.VALID or invite.protocol_id is None:
            await self._refuse(INVITE_INVALID)

        if await self.users.get_by_telegram_id(data.telegram_id) or await self.registrations.get_open_by_telegram_id(
            data.telegram_id
        ):
            await self._refuse(TELEGRAM_TAKEN)
        if await self.users.get_by_email_ignore_case(data.email):
            await self._refuse(EMAIL_TAKEN)

        registration = await self.registrations.create(
            BotRegistration(
                invite_id=invite.id,
                bot_key_id=bot_key_id,
                telegram_id=data.telegram_id,
                telegram_username=data.telegram_username,
                name=data.name,
                email=data.email,
                status=RegistrationStatus.PAYMENT_PENDING,
            )
        )
        invite.used_at = now
        self.audit.record(
            AuditAction.BOT_REGISTRATION_CREATED,
            AuditEntity.BOT_REGISTRATION,
            registration.id,
            {"invite_id": invite.id, "bot_key_id": bot_key_id, "days": invite.days, "protocol_id": invite.protocol_id},
        )
        if await self.payment.check(registration) is PaymentStatus.PENDING:
            await self.session.commit()
            return registration

        try:
            grant_id = await self._stage_access(registration, invite.protocol_id, invite.days, now)
        except NotFoundError:  # протокол удалили, пока шла регистрация
            await self._refuse(INVITE_INVALID, staged=True)
        # Один commit на всё: регистрация, приглашение, пользователь, подписка, грант и задача - или ничего.
        await VPNCredentialService(self.session, self.actor).schedule_issue(grant_id)
        return registration

    async def _stage_access(self, registration: BotRegistration, protocol_id: UUID, days: int, now: datetime) -> UUID:
        """Пользователь, подписка на days дней и грант на протокол - сервисами панели, без commit. Id гранта."""
        user = await VPNUserService(self.session, self.actor).stage_create(
            VPNUserCreate(username=registration.name, email=registration.email),
            fields={
                "telegram_id": registration.telegram_id,
                "telegram_username": registration.telegram_username,
                "status": VPNUserStatus.ACTIVE,
            },
            audit_meta={"source": "bot"},  # без почты и telegram_id: аудит не хранит персональные данные
        )
        subscription = await SubscriptionService(self.session, self.actor).stage_create(
            user.id, SubscriptionCreate(valid_from=now, valid_until=now + timedelta(days=days))
        )
        grant = await AccessGrantService(self.session, self.actor).stage_create(
            subscription.id, AccessGrantCreate(protocol_id=protocol_id)
        )
        registration.user_id = user.id
        registration.access_grant_id = grant.id
        registration.status = RegistrationStatus.PROVISIONING
        await self.session.flush()
        return grant.id

    async def _refuse(self, reason: str, *, staged: bool = False) -> NoReturn:
        """Отказ. До создания чего-либо в транзакции только блокировка приглашения: commit её снимает и не
        трогает объекты сессии вызывающего. Если уже создано (staged), всё откатывается."""
        if staged:
            await self.session.rollback()
        else:
            await self.session.commit()
        raise RegistrationConflictError(reason)

    # --- чтение ----------------------------------------------------------------------------------------------------

    async def get(self, registration_id: UUID) -> BotRegistration:
        registration = await self.registrations.get_by_id(registration_id)
        if registration is None:
            raise NotFoundError(f"Registration '{registration_id}' not found")
        return registration

    async def list(self, status: RegistrationStatus | None = None) -> Sequence[BotRegistration]:
        return await self.registrations.list_all(status)

    async def by_telegram(self, telegram_id: int) -> UserStatusRead:
        """/status в боте: имя, до какого числа доступ и на каком протоколе."""
        user = await self.users.get_by_telegram_id(telegram_id)
        if user is None:
            raise NotFoundError("User not found")
        subscription = await SubscriptionRepository(self.session).get_latest_by_user_id(user.id)
        protocol = None
        if subscription is not None:
            grants = await AccessGrantRepository(self.session).list_by_subscription_id(subscription.id)
            if grants:
                found = await ProtocolRepository(self.session).get_by_id(grants[0].protocol_id)
                protocol = found.type.value if found else None
        return UserStatusRead(
            name=user.username, valid_until=subscription.valid_until if subscription else None, protocol=protocol
        )

    # --- итог выдачи -----------------------------------------------------------------------------------------------

    async def on_issue_finished(self, job: Job) -> None:
        """Воркер вызывает на завершённой задаче CREATE_USER до финального commit, в одной транзакции со статусом.

        Регистрация по этому гранту (если она есть и ждёт выдачи) становится active или failed. Грант не из бота:
        один запрос по уникальному индексу, больше ничего.
        """
        if job.type != JobType.CREATE_USER or job.access_grant_id is None:
            return
        registration = await self.registrations.get_by_grant_id(job.access_grant_id, lock=True)
        if registration is None or registration.status != RegistrationStatus.PROVISIONING:
            return
        if job.status == JobStatus.SUCCESS:
            self._activate(registration)
        elif job.status == JobStatus.FAILED:
            self._fail(registration, job.error, job.id)

    def _activate(self, registration: BotRegistration) -> None:
        registration.status = RegistrationStatus.ACTIVE
        registration.error = None
        registration.finished_at = datetime.now(timezone.utc)
        self.audit.record(AuditAction.BOT_REGISTRATION_ACTIVATED, AuditEntity.BOT_REGISTRATION, registration.id)

    def _fail(self, registration: BotRegistration, error: str | None, job_id: UUID | None = None) -> None:
        # error - уже текст задачи (job_error_text: секреты замаскированы, длина ограничена).
        registration.status = RegistrationStatus.FAILED
        registration.error = (error or _INTERRUPTED)[:_ERROR_MAX_LENGTH]
        registration.finished_at = datetime.now(timezone.utc)
        self.audit.record(
            AuditAction.BOT_REGISTRATION_FAILED,
            AuditEntity.BOT_REGISTRATION,
            registration.id,
            {"job_id": job_id} if job_id else None,
        )

    async def reconcile(self) -> int:
        """Страховка (правило stale_jobs): регистрации в provisioning, у которых выдача уже не идёт.

        Хук воркера не вызывается, если воркер убит, задачу закрыл _reject или грант удалили вместе с пользователем:
        без этого регистрация осталась бы в provisioning навсегда.
        """
        jobs = JobRepository(self.session)
        credentials = VPNCredentialRepository(self.session)
        changed = 0
        for registration in await self.registrations.list_provisioning():
            grant_id = registration.access_grant_id
            if grant_id is None:
                self._fail(registration, None)
            elif await jobs.has_unfinished_for_grant(JobType.CREATE_USER, grant_id):
                continue
            elif await credentials.get_live_by_access_grant_id(grant_id):
                self._activate(registration)
            else:
                last = await jobs.get_last_for_grant(JobType.CREATE_USER, grant_id)
                self._fail(registration, last.error if last else None, last.id if last else None)
            changed += 1
        await self.session.commit()
        return changed

    # --- повтор выдачи админом -------------------------------------------------------------------------------------

    async def retry(self, registration_id: UUID) -> BotRegistration:
        """Повторить выдачу упавшей регистрации: тот же пользователь и грант, новая задача CREATE_USER."""
        registration = await self.registrations.get_by_id(registration_id, lock=True)
        if registration is None:
            await self.session.rollback()
            raise NotFoundError(f"Registration '{registration_id}' not found")
        grant_id = registration.access_grant_id
        if registration.status != RegistrationStatus.FAILED:
            await self.session.rollback()
            raise StateConflictError("Only a failed registration can be retried")
        if grant_id is None or registration.user_id is None:
            await self.session.rollback()
            raise StateConflictError("The user or the access of this registration was removed: issue it manually")

        if await VPNCredentialRepository(self.session).get_live_by_access_grant_id(grant_id):
            # Клиент уже есть (задачу закрыли как упавшую, но выдача успела): это не сбой.
            self._activate(registration)
            await self.session.commit()
            return registration

        registration.status = RegistrationStatus.PROVISIONING
        registration.error = None
        registration.finished_at = None
        self.audit.record(AuditAction.BOT_REGISTRATION_RETRIED, AuditEntity.BOT_REGISTRATION, registration.id)
        try:
            await VPNCredentialService(self.session, self.actor).schedule_issue(grant_id)
        except AppError:
            await self.session.rollback()  # доступ отозван / выдача уже идёт: статус остаётся failed
            raise
        return registration
