#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, and_, delete, func, literal, not_, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncSession

from modules.access.models import AccessGrantStatus, CredentialSource
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.bot.models.broadcast import BotBroadcast
from modules.bot.models.config_link import ConfigLink
from modules.bot.models.invite import BotInvite
from modules.bot.models.key import BotKey
from modules.bot.models.outbox import OPEN_STATUSES as OUTBOX_OPEN_STATUSES
from modules.bot.models.outbox import BotOutbox, OutboxStatus
from modules.bot.models.registration import OPEN_STATUSES, BotRegistration, RegistrationStatus
from modules.bot.models.settings import BotSettings
from modules.bot.schemas import AudienceRequest, BroadcastCreate
from modules.protocols.models.protocol import VPNProtocol
from modules.servers.models.server import VPNServer
from modules.vpn_users.models.user import VPNUser


class BotKeyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, key: BotKey) -> BotKey:
        self.session.add(key)
        await self.session.flush()
        return key

    async def get_by_id(self, key_id: UUID, *, lock: bool = False) -> BotKey | None:
        return await self.session.get(BotKey, key_id, with_for_update=lock, populate_existing=lock)

    async def get_by_hash(self, key_hash: str) -> BotKey | None:
        """Ключ по хешу, в том числе отозванный (повторный revoke из консоли не считается ошибкой)."""
        query = select(BotKey).where(BotKey.key_hash == key_hash)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_active_by_hash(self, key_hash: str) -> BotKey | None:
        query = select(BotKey).where(BotKey.key_hash == key_hash, BotKey.revoked_at.is_(None))
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_all(self) -> Sequence[BotKey]:
        return (await self.session.execute(select(BotKey).order_by(BotKey.created_at.desc()))).scalars().all()

    async def latest_bot_username(self) -> str | None:
        """Имя бота, которое сообщил последний замеченный действующий ключ: для ссылки t.me в приглашении."""
        query = (
            select(BotKey.bot_username)
            .where(BotKey.revoked_at.is_(None), BotKey.bot_username.is_not(None))
            .order_by(BotKey.last_seen_at.desc())
            .limit(1)
        )
        return (await self.session.execute(query)).scalar_one_or_none()


class BotInviteRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, invite: BotInvite) -> BotInvite:
        self.session.add(invite)
        await self.session.flush()
        return invite

    async def get_by_id(self, invite_id: UUID) -> BotInvite | None:
        return await self.session.get(BotInvite, invite_id)

    async def get_by_hash(self, code_hash: str, *, lock: bool = False) -> BotInvite | None:
        """lock=True: строка блокируется до конца транзакции (свежие данные из БД): приглашение занимает один запрос."""
        query = select(BotInvite).where(BotInvite.code_hash == code_hash)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_all(self) -> Sequence[BotInvite]:
        return (await self.session.execute(select(BotInvite).order_by(BotInvite.created_at.desc()))).scalars().all()


class BotRegistrationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, registration: BotRegistration) -> BotRegistration:
        self.session.add(registration)
        await self.session.flush()
        return registration

    async def get_by_id(self, registration_id: UUID, *, lock: bool = False) -> BotRegistration | None:
        return await self.session.get(BotRegistration, registration_id, with_for_update=lock, populate_existing=lock)

    async def get_by_invite_id(self, invite_id: UUID) -> BotRegistration | None:
        query = select(BotRegistration).where(BotRegistration.invite_id == invite_id)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_by_grant_id(self, access_grant_id: UUID, *, lock: bool = False) -> BotRegistration | None:
        query = select(BotRegistration).where(BotRegistration.access_grant_id == access_grant_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_open_by_telegram_id(self, telegram_id: int) -> BotRegistration | None:
        query = select(BotRegistration).where(
            BotRegistration.telegram_id == telegram_id, BotRegistration.status.in_(OPEN_STATUSES)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_all(self, status: RegistrationStatus | None = None) -> Sequence[BotRegistration]:
        query = select(BotRegistration).order_by(BotRegistration.created_at.desc())
        if status is not None:
            query = query.where(BotRegistration.status == status)
        return (await self.session.execute(query)).scalars().all()

    async def list_provisioning(self) -> Sequence[BotRegistration]:
        """Регистрации, ждущие выдачи клиента, под блокировкой строк (занятые другой транзакцией пропускаются)."""
        query = (
            select(BotRegistration)
            .where(BotRegistration.status == RegistrationStatus.PROVISIONING)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(query)).scalars().all()


class ConfigLinkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, link: ConfigLink) -> ConfigLink:
        self.session.add(link)
        await self.session.flush()
        return link

    async def credentials_of_user(self, user_id: UUID) -> list[tuple[VPNCredential, AccessGrant, Subscription]]:
        """Не отозванные клиенты человека вместе с доступом и подпиской (годность доступа решает вызывающий)."""
        query = (
            select(VPNCredential, AccessGrant, Subscription)
            .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .where(Subscription.user_id == user_id, VPNCredential.revoked_at.is_(None))
            .execution_options(populate_existing=True)
        )
        rows = (await self.session.execute(query)).all()
        return [(credential, grant, subscription) for credential, grant, subscription in rows]

    async def grants_of_user(self, user_id: UUID) -> list[tuple[AccessGrant, Subscription]]:
        query = (
            select(AccessGrant, Subscription)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .where(Subscription.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        return [(grant, subscription) for grant, subscription in (await self.session.execute(query)).all()]

    async def registered_grant_id(self, telegram_id: int) -> UUID | None:
        """Доступ последней завершённой регистрации через бота этого человека (его клиент важнее более нового)."""
        query = (
            select(BotRegistration.access_grant_id)
            .where(
                BotRegistration.telegram_id == telegram_id,
                BotRegistration.status == RegistrationStatus.ACTIVE,
                BotRegistration.access_grant_id.is_not(None),
            )
            .order_by(BotRegistration.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def lock_credential(self, credential_id: UUID) -> VPNCredential | None:
        """Клиент под блокировкой строки до конца транзакции, со свежими данными из БД."""
        query = (
            select(VPNCredential)
            .where(VPNCredential.id == credential_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def live_links(self, credential_id: UUID) -> list[ConfigLink]:
        query = (
            select(ConfigLink)
            .where(ConfigLink.credential_id == credential_id, ConfigLink.revoked_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(query)).scalars())

    async def valid_until_of(self, credential_id: UUID) -> datetime | None:
        """Конец подписки, по которой выдан клиент."""
        query = (
            select(Subscription.valid_until)
            .join(AccessGrant, AccessGrant.subscription_id == Subscription.id)
            .join(VPNCredential, VPNCredential.access_grant_id == AccessGrant.id)
            .where(VPNCredential.id == credential_id)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def open_target(
        self, token_hash: str
    ) -> tuple[ConfigLink, VPNCredential, AccessGrant, Subscription, str, str] | None:
        """Всё для страницы по хешу токена одним запросом: живая ссылка, не отозванный выданный клиент с конфигом, его
        доступ, подписка, имя человека и имя сервера. Годность доступа по датам и статусам решает вызывающий."""
        query = (
            select(ConfigLink, VPNCredential, AccessGrant, Subscription, VPNUser.username, VPNServer.name)
            .join(VPNCredential, ConfigLink.credential_id == VPNCredential.id)
            .join(AccessGrant, VPNCredential.access_grant_id == AccessGrant.id)
            .join(Subscription, AccessGrant.subscription_id == Subscription.id)
            .join(VPNUser, Subscription.user_id == VPNUser.id)
            .join(VPNServer, AccessGrant.server_id == VPNServer.id)
            .where(
                ConfigLink.token_hash == token_hash,
                ConfigLink.revoked_at.is_(None),
                VPNCredential.revoked_at.is_(None),
                VPNCredential.source == CredentialSource.ISSUED,
                VPNCredential.encrypted_config.is_not(None),
            )
            .execution_options(populate_existing=True)
        )
        row = (await self.session.execute(query)).first()
        if row is None:
            return None
        link, credential, grant, subscription, username, server_name = row
        return link, credential, grant, subscription, username, server_name


def _rowcount(result: Result[Any]) -> int:
    return int(cast("CursorResult[Any]", result).rowcount or 0)


_DEAD_ACCESS = (AccessGrantStatus.EXPIRED, AccessGrantStatus.REVOKED)
_OPEN = [status.value for status in OUTBOX_OPEN_STATUSES]


class BotSettingsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(self) -> BotSettings:
        """Строка настроек (id = 1). Миграция её вставляет; здесь - на случай, когда её нет (чистая тестовая БД)."""
        await self.session.execute(pg_insert(BotSettings).values(id=1).on_conflict_do_nothing())
        row = (
            await self.session.execute(
                select(BotSettings).where(BotSettings.id == 1).execution_options(populate_existing=True)
            )
        ).scalar_one()
        return row


class OutboxRepository:
    """Исходящая очередь бота и рассылки. Счётчики рассылки меняются в той же транзакции, что и статусы строк."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # --- люди ------------------------------------------------------------------------------------------------------

    async def unmark_blocked(self, telegram_id: int) -> bool:
        """Человек сам написал боту: пометка «заблокировал бота» снимается. Идемпотентно; True - сняли. Без commit."""
        result = await self.session.execute(
            update(VPNUser)
            .where(VPNUser.telegram_id == telegram_id, VPNUser.bot_blocked_at.is_not(None))
            .values(bot_blocked_at=None)
        )
        return _rowcount(result) > 0

    async def fresh_user(self, user_id: UUID) -> VPNUser | None:
        """Человек со свежими данными из БД (пометка «заблокировал» могла смениться в другой транзакции)."""
        query = select(VPNUser).where(VPNUser.id == user_id).execution_options(populate_existing=True)
        return (await self.session.execute(query)).scalar_one_or_none()

    def _targets(self, data: AudienceRequest, now: datetime) -> Select[Any]:
        """Кому адресована рассылка: (user_id, grant_id, telegram_id, bot_blocked_at) - без фильтра по Telegram."""
        if data.target == "protocol":
            return (
                select(
                    VPNUser.id.label("user_id"),
                    AccessGrant.id.label("grant_id"),
                    VPNUser.telegram_id,
                    VPNUser.bot_blocked_at,
                )
                .join(Subscription, Subscription.user_id == VPNUser.id)
                .join(AccessGrant, AccessGrant.subscription_id == Subscription.id)
                .where(
                    AccessGrant.protocol_id == data.protocol_id,
                    AccessGrant.status.not_in(_DEAD_ACCESS),
                    Subscription.status.not_in(_DEAD_ACCESS),
                    Subscription.valid_from <= now,
                    Subscription.valid_until > now,
                )
                .distinct(VPNUser.id)
                .order_by(VPNUser.id, AccessGrant.created_at)
            )
        query: Select[Any] = select(
            VPNUser.id.label("user_id"),
            literal(None, type_=AccessGrant.id.type).label("grant_id"),
            VPNUser.telegram_id,
            VPNUser.bot_blocked_at,
        )
        if data.target == "selected":
            query = query.where(VPNUser.id.in_(data.user_ids or []))
        return query

    async def audience(self, data: AudienceRequest, now: datetime) -> tuple[int, int, int]:
        """(получателей, без Telegram, заблокировавших бота) среди адресатов рассылки."""
        targets = self._targets(data, now).subquery()
        query = select(
            func.count().filter(and_(targets.c.telegram_id.is_not(None), targets.c.bot_blocked_at.is_(None))),
            func.count().filter(targets.c.telegram_id.is_(None)),
            func.count().filter(and_(targets.c.telegram_id.is_not(None), targets.c.bot_blocked_at.is_not(None))),
        )
        recipients, without_telegram, blocked = (await self.session.execute(query)).one()
        return int(recipients), int(without_telegram), int(blocked)

    async def get_broadcast_by_request(self, request_id: UUID) -> BotBroadcast | None:
        query = select(BotBroadcast).where(BotBroadcast.request_id == request_id)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_broadcast(self, broadcast_id: UUID, *, lock: bool = False) -> BotBroadcast | None:
        return await self.session.get(BotBroadcast, broadcast_id, with_for_update=lock, populate_existing=lock)

    async def broadcasts(self, ids: Iterable[UUID]) -> dict[UUID, BotBroadcast]:
        found = (
            await self.session.execute(
                select(BotBroadcast).where(BotBroadcast.id.in_(list(ids))).execution_options(populate_existing=True)
            )
        ).scalars()
        return {broadcast.id: broadcast for broadcast in found}

    async def create_broadcast(self, broadcast: BotBroadcast) -> BotBroadcast:
        self.session.add(broadcast)
        await self.session.flush()
        return broadcast

    async def queue_broadcast(self, broadcast: BotBroadcast, data: BroadcastCreate, now: datetime) -> int:
        """Строки очереди одним INSERT ... SELECT; кому уже есть открытая отправка ссылки, тому вторая не ставится
        (частичный уникальный индекс). Возвращает число поставленных строк."""
        targets = (
            self._targets(data, now)
            .where(VPNUser.telegram_id.is_not(None), VPNUser.bot_blocked_at.is_(None))
            .subquery()
        )
        priority = 10
        pick = select(
            func.gen_random_uuid(),
            literal(broadcast.kind),
            literal(broadcast.id),
            targets.c.user_id,
            targets.c.grant_id,
            literal(OutboxStatus.PENDING.value),
            literal(priority),
            literal(0),
            func.now(),
            func.now(),
        )
        statement = (
            pg_insert(BotOutbox)
            .from_select(
                [
                    "id",
                    "kind",
                    "broadcast_id",
                    "user_id",
                    "access_grant_id",
                    "status",
                    "priority",
                    "attempts",
                    "created_at",
                    "updated_at",
                ],
                pick,
            )
            .on_conflict_do_nothing()
        )
        result = await self.session.execute(statement)
        return _rowcount(result)

    async def count_targets(self, data: BroadcastCreate, now: datetime) -> int:
        recipients, _, _ = await self.audience(data, now)
        return recipients

    # --- строки очереди --------------------------------------------------------------------------------------------

    async def get_row(self, outbox_id: UUID) -> BotOutbox | None:
        return await self.session.get(BotOutbox, outbox_id, populate_existing=True)

    async def open_config_row(self, user_id: UUID) -> BotOutbox | None:
        query = (
            select(BotOutbox)
            .where(BotOutbox.user_id == user_id, BotOutbox.kind == "config", BotOutbox.status.in_(_OPEN))
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(query)).scalars().first()

    async def add_row(self, row: BotOutbox) -> BotOutbox:
        self.session.add(row)
        await self.session.flush()
        return row

    async def lease_candidates(self, now: datetime, limit: int) -> list[BotOutbox]:
        """Строки, которые можно выдать: готовые к отправке, и выданные, у которых аренда вышла. Строки, занятые другой
        выдачей, пропускаются (SKIP LOCKED): две выдачи одновременно получают разные строки."""
        ready = and_(
            BotOutbox.status == OutboxStatus.PENDING.value,
            or_(BotOutbox.not_before.is_(None), BotOutbox.not_before <= now),
        )
        expired = and_(BotOutbox.status == OutboxStatus.CLAIMED.value, BotOutbox.claimed_until < now)
        query = (
            select(BotOutbox)
            .where(or_(ready, expired))
            .order_by(BotOutbox.priority, BotOutbox.created_at, BotOutbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(query)).scalars())

    async def users_by_ids(self, ids: Iterable[UUID]) -> dict[UUID, VPNUser]:
        found = (
            await self.session.execute(
                select(VPNUser).where(VPNUser.id.in_(list(ids))).execution_options(populate_existing=True)
            )
        ).scalars()
        return {user.id: user for user in found}

    async def leased_rows(self, lease_id: UUID, ids: Iterable[UUID]) -> dict[UUID, BotOutbox]:
        """Выданные по этой аренде строки из списка, под блокировкой (порядок по id: параллельные ack не зацепятся)."""
        query = (
            select(BotOutbox)
            .where(
                BotOutbox.id.in_(list(ids)),
                BotOutbox.lease_id == lease_id,
                BotOutbox.status == OutboxStatus.CLAIMED.value,
            )
            .order_by(BotOutbox.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return {row.id: row for row in (await self.session.execute(query)).scalars()}

    async def cancel_waiting(self, broadcast_id: UUID, now: datetime) -> int:
        """Ждущие строки рассылки закрываются; выданные доходят (у выданных с истёкшей арендой выдача ещё раз не
        будет: claim отменяет и их)."""
        result = await self.session.execute(
            update(BotOutbox)
            .where(
                BotOutbox.broadcast_id == broadcast_id,
                or_(
                    BotOutbox.status == OutboxStatus.PENDING.value,
                    and_(BotOutbox.status == OutboxStatus.CLAIMED.value, BotOutbox.claimed_until < now),
                ),
            )
            .values(status=OutboxStatus.CANCELLED.value, lease_id=None, claimed_until=None)
        )
        return _rowcount(result)

    async def add_counts(self, broadcast_id: UUID, **deltas: int) -> None:
        """Счётчики рассылки одним UPDATE: sent=sent+n и т.д."""
        values = {name: getattr(BotBroadcast, name) + delta for name, delta in deltas.items() if delta}
        if values:
            await self.session.execute(update(BotBroadcast).where(BotBroadcast.id == broadcast_id).values(**values))

    async def finish_if_done(self, broadcast_ids: Iterable[UUID], now: datetime) -> None:
        """Рассылка завершена, когда у неё не осталось открытых строк."""
        ids = list(broadcast_ids)
        if not ids:
            return
        still_open = (
            select(BotOutbox.id).where(BotOutbox.broadcast_id == BotBroadcast.id, BotOutbox.status.in_(_OPEN)).exists()
        )
        await self.session.execute(
            update(BotBroadcast)
            .where(BotBroadcast.id.in_(ids), BotBroadcast.finished_at.is_(None), not_(still_open))
            .values(finished_at=now)
        )

    async def purge_finished(self, older_than_days: int, now: datetime) -> int:
        """Завершённые строки старше срока удаляются; открытые остаются, итоги рассылок - тоже."""
        border = now - timedelta(days=older_than_days)
        result = await self.session.execute(
            delete(BotOutbox).where(BotOutbox.status.not_in(_OPEN), BotOutbox.updated_at < border)
        )
        return _rowcount(result)


class BotPageRepository:
    """Запросы страницы «Бот»: состояние, получатели, протоколы, списки с курсором (новые первыми)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def keys(self) -> Sequence[BotKey]:
        return (await self.session.execute(select(BotKey))).scalars().all()

    async def outbox_counts(self) -> tuple[int, int]:
        query = select(
            func.count().filter(BotOutbox.status == OutboxStatus.PENDING.value),
            func.count().filter(BotOutbox.status == OutboxStatus.CLAIMED.value),
        )
        pending, claimed = (await self.session.execute(query)).one()
        return int(pending), int(claimed)

    async def recipients(self, search: str | None, limit: int) -> Sequence[VPNUser]:
        query = select(VPNUser).where(VPNUser.telegram_id.is_not(None))
        if search:
            # autoescape: «%» и «_» в поиске - обычные символы, а не шаблон
            query = query.where(
                or_(
                    VPNUser.username.icontains(search, autoescape=True),
                    VPNUser.telegram_username.icontains(search, autoescape=True),
                )
            )
        return (await self.session.execute(query.order_by(VPNUser.username, VPNUser.id).limit(limit))).scalars().all()

    async def protocols(self) -> Sequence[tuple[VPNProtocol, str]]:
        query = (
            select(VPNProtocol, VPNServer.name)
            .join(VPNServer, VPNProtocol.server_id == VPNServer.id)
            .order_by(VPNServer.name, VPNProtocol.type)
        )
        return [(protocol, name) for protocol, name in (await self.session.execute(query)).all()]

    async def invites(
        self, limit: int, before: tuple[datetime, UUID] | None
    ) -> Sequence[tuple[BotInvite, UUID | None, str | None]]:
        """Приглашения вместе с тем, кто их занял (регистрация, а по ней человек)."""
        query = (
            select(BotInvite, BotRegistration.user_id, VPNUser.username)
            .outerjoin(BotRegistration, BotRegistration.invite_id == BotInvite.id)
            .outerjoin(VPNUser, VPNUser.id == BotRegistration.user_id)
        )
        if before:
            query = query.where(
                tuple_(BotInvite.created_at, BotInvite.id) < tuple_(literal(before[0]), literal(before[1]))
            )
        query = query.order_by(BotInvite.created_at.desc(), BotInvite.id.desc()).limit(limit)
        return [(invite, user_id, name) for invite, user_id, name in (await self.session.execute(query)).all()]

    async def registrations(
        self, status: RegistrationStatus | None, limit: int, before: tuple[datetime, UUID] | None
    ) -> Sequence[BotRegistration]:
        query = select(BotRegistration)
        if status is not None:
            query = query.where(BotRegistration.status == status)
        if before:
            query = query.where(
                tuple_(BotRegistration.created_at, BotRegistration.id) < tuple_(literal(before[0]), literal(before[1]))
            )
        query = query.order_by(BotRegistration.created_at.desc(), BotRegistration.id.desc()).limit(limit)
        return (await self.session.execute(query)).scalars().all()

    async def open_broadcast_ids(self, ids: Iterable[UUID]) -> set[UUID]:
        """Рассылки из списка, у которых ещё есть ждущие или выданные боту строки (удалённые люди их не оставляют)."""
        query = (
            select(BotOutbox.broadcast_id)
            .where(BotOutbox.broadcast_id.in_(list(ids)), BotOutbox.status.in_(_OPEN))
            .distinct()
        )
        return {broadcast_id for broadcast_id in (await self.session.execute(query)).scalars() if broadcast_id}

    async def broadcasts(self, limit: int, before: tuple[datetime, UUID] | None) -> Sequence[BotBroadcast]:
        query = select(BotBroadcast)
        if before:
            query = query.where(
                tuple_(BotBroadcast.created_at, BotBroadcast.id) < tuple_(literal(before[0]), literal(before[1]))
            )
        query = query.order_by(BotBroadcast.created_at.desc(), BotBroadcast.id.desc()).limit(limit)
        return (await self.session.execute(query)).scalars().all()
