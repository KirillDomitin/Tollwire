#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import AddressValueError, IPv4Address, IPv4Network
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.exceptions import InvalidDataError, JobAlreadyRunningError, NotFoundError, StateConflictError
from infrastructure.geoip import DownloadError, download_text
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.gateway.repository import GatewayExitRepository
from modules.geoip.models.geoip import GeoipList, GeoipOverride, GeoipStatus, OverrideAction
from modules.geoip.parser import (
    GeoipError,
    ParsedList,
    addresses_count,
    effective_list,
    parse_list,
    parse_stored,
    render_list,
    requires_confirmation,
    sha256_of,
    validate_override,
)
from modules.geoip.repository import GeoipRepository
from modules.geoip.schemas import GeoipListRead, GeoipOverview, OverrideCreate
from modules.jobs.models.job import Job, JobType
from modules.jobs.repository import JobRepository
from modules.jobs.service import JobService
from modules.protocols.models.protocol import ProtocolType
from modules.protocols.repository import ProtocolRepository
from modules.servers.repository import CredentialRepository, ServerRepository

logger = logging.getLogger("geoip")

Downloader = Callable[[str, int, int], Awaitable[str]]


@dataclass(frozen=True)
class _Prepared:
    """Результат тяжёлой (CPU) части приёма списка; считается в потоке, чтобы не блокировать event loop API."""

    parsed: ParsedList
    data: str
    sha256: str
    addresses: int
    confirmation: bool  # отличие от активного больше допустимого


def _prepare(text: str, active_data: str | None, strict: bool) -> _Prepared:
    parsed = parse_list(text, strict=strict)
    data = render_list(parsed.prefixes)
    confirmation = False
    if active_data is not None:
        confirmation = requires_confirmation(parse_stored(active_data), parsed.prefixes)
    return _Prepared(parsed, data, sha256_of(data), addresses_count(parsed.prefixes), confirmation)


def _effective(
    active_data: str, direct: list[IPv4Network], tunnel: list[IPv4Network], protected: list[IPv4Network]
) -> list[IPv4Network]:
    return effective_list(parse_stored(active_data), direct, tunnel, protected)


class GeoipService:
    """Список российских префиксов для «России напрямую» на шлюзе (этап 24.4): получение, проверка, активация,
    исключения администратора и постановка применения на шлюзы.

    Список принимается, только если прошёл строгий разбор (`parser.parse_list`). Первый список и список, отличающийся от
    активного не больше чем на 20% (по числу префиксов и по покрытым адресам), активируется сам; больший скачок
    сохраняется кандидатом и ждёт подтверждения админа (`activate`): подмена или поломка источника не должна
    молча перестроить маршрутизацию всех шлюзов.
    """

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
        self.repository = GeoipRepository(session)
        self.jobs = JobService(session, actor)
        self.audit = AuditService(session, actor)

    # --- чтение ------------------------------------------------------------------------------------------------

    async def overview(self) -> GeoipOverview:
        active = await self.repository.get_active()
        candidates = await self.repository.list_candidates()
        return GeoipOverview(
            active=GeoipListRead.model_validate(active) if active else None,
            candidates=[GeoipListRead.model_validate(item) for item in candidates],
        )

    async def list_overrides(self) -> list[GeoipOverride]:
        return list(await self.repository.list_overrides())

    async def protected_networks(self, gateway_server_id: UUID | None = None) -> list[IPv4Network]:
        """Адреса (/32) шлюзов и их выходов: через них идут туннели и управление, «напрямую» их закрывать нельзя.
        `gateway_server_id` - только этот шлюз и его выходы; None - все шлюзы панели. Адрес-имя (не IPv4) пропускается:
        панель не резолвит имена, поэтому такой сервер от DIRECT-исключения не защищён."""
        servers, exits = ServerRepository(self.session), GatewayExitRepository(self.session)
        hosts: list[str] = []
        for protocol in await ProtocolRepository(self.session).list_by_type(ProtocolType.AMNEZIA_GATEWAY):
            if gateway_server_id is not None and protocol.server_id != gateway_server_id:
                continue
            server = await servers.get_by_id(protocol.server_id)
            if server is not None:
                hosts.append(server.host)
            hosts.extend(await exits.exit_hosts(protocol.id))
        networks: set[IPv4Network] = set()
        for host in hosts:
            try:
                networks.add(IPv4Network(IPv4Address(host.strip())))
            except (AddressValueError, ValueError):
                continue
        return sorted(networks)

    async def effective_prefixes(self, gateway_server_id: UUID | None = None) -> list[IPv4Network] | None:
        """Эффективный список: ((активный ∪ DIRECT) − зарезервированные − TUNNEL − адреса шлюза и его выходов). None -
        активного списка нет (шлюзам нечего применять). Испорченные данные - GeoipError / ValueError."""
        active = await self.repository.get_active()
        if active is None:
            return None
        direct: list[IPv4Network] = []
        tunnel: list[IPv4Network] = []
        for override in await self.repository.list_overrides():
            target = direct if override.action == OverrideAction.DIRECT else tunnel
            target.append(IPv4Network(override.network))
        protected = await self.protected_networks(gateway_server_id)
        return await asyncio.to_thread(_effective, active.data, direct, tunnel, protected)

    # --- получение списка --------------------------------------------------------------------------------------

    async def schedule_update(self) -> Job:
        """Ставит GEOIP_UPDATE (202). Глобальная задача без сервера; незавершённая такая же - 409."""
        if await JobRepository(self.session).has_unfinished_of_type(JobType.GEOIP_UPDATE):
            raise JobAlreadyRunningError("The geoip list is already being updated")
        return await self.jobs.create(JobType.GEOIP_UPDATE, server_id=None)

    async def run_update(self, downloader: Downloader | None = None) -> dict[str, Any]:
        """Скачивает список по адресу из настроек и принимает его. Исполняет воркер."""
        fetch = downloader or (lambda url, limit, timeout: download_text(url, limit, timeout))
        try:
            text = await fetch(
                settings.geoip_source_url, settings.geoip_download_max_bytes, settings.geoip_download_timeout_seconds
            )
        except DownloadError as exc:
            raise InvalidDataError(str(exc)) from None
        return await self.ingest(text, "ripe")

    async def upload(self, text: str) -> dict[str, Any]:
        """Загрузка админа (RIPE или CIDR): те же проверки и те же правила активации."""
        return await self.ingest(text, "upload")

    async def ingest(self, text: str, source: str) -> dict[str, Any]:
        # Активный список блокируется до конца приёма: два одновременных приёма идут по очереди. Если оба видели «списка
        # нет», второго остановит частичный уникальный индекс (IntegrityError -> 409).
        active = await self.repository.get_active(lock=True)
        try:
            prepared = await asyncio.to_thread(_prepare, text, active.data if active else None, source == "upload")
        except GeoipError as exc:
            raise InvalidDataError(str(exc)) from None
        except ValueError:  # испорченный сохранённый активный список: текст ошибки содержал бы его строки
            raise InvalidDataError("The stored geoip list is damaged") from None
        parsed = prepared.parsed
        summary: dict[str, Any] = {
            "version": parsed.version,
            "prefixes": len(parsed.prefixes),
            "sha256": prepared.sha256,
            "skipped": parsed.skipped,
            "rejected": parsed.rejected,
        }
        if active is not None and active.sha256 == prepared.sha256:
            # Тот же список, что активный: ждущий подтверждения кандидат устарел.
            await self.repository.supersede_candidates()
            await self.session.commit()
            return {**summary, "status": "unchanged", "list_id": str(active.id)}
        waiting = await self.repository.find_candidate_by_hash(prepared.sha256)
        if waiting is not None:
            return {**summary, "status": "candidate", "list_id": str(waiting.id)}

        automatic = active is None or not prepared.confirmation
        try:
            if automatic and active is not None:
                active.status = GeoipStatus.SUPERSEDED  # раньше вставки нового: уникальный индекс допускает один ACTIVE
                await self.session.flush()
            geoip_list = await self.repository.add(
                GeoipList(
                    source=source,
                    version=parsed.version,
                    sha256=prepared.sha256,
                    prefixes_count=len(parsed.prefixes),
                    addresses_count=prepared.addresses,
                    data=prepared.data,
                    status=GeoipStatus.ACTIVE if automatic else GeoipStatus.CANDIDATE,
                    activated_at=datetime.now(timezone.utc) if automatic else None,
                )
            )
            await self.repository.supersede_candidates(except_id=geoip_list.id)
            self._audit_list(AuditAction.GEOIP_LIST_UPDATED, geoip_list, source=source)
            if automatic:
                self._audit_list(AuditAction.GEOIP_LIST_ACTIVATED, geoip_list)
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise StateConflictError("The geoip list was changed by another request, try again") from None
        if automatic:
            await self.schedule_apply_all()
        return {**summary, "status": geoip_list.status.value, "list_id": str(geoip_list.id)}

    async def activate(self, list_id: UUID) -> GeoipList:
        """Админ подтверждает кандидата: он становится активным, прежний - SUPERSEDED, шлюзам ставится применение."""
        geoip_list = await self.repository.get(list_id)
        if geoip_list is None:
            raise NotFoundError("Geoip list not found")
        if geoip_list.status != GeoipStatus.CANDIDATE:
            raise StateConflictError("Only a candidate list can be activated")
        active = await self.repository.get_active(lock=True)
        try:
            if active is not None:
                active.status = GeoipStatus.SUPERSEDED
                await self.session.flush()
            geoip_list.status = GeoipStatus.ACTIVE
            geoip_list.activated_at = datetime.now(timezone.utc)
            await self.repository.supersede_candidates(except_id=geoip_list.id)
            self._audit_list(AuditAction.GEOIP_LIST_ACTIVATED, geoip_list)
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise StateConflictError("The geoip list was changed by another request, try again") from None
        await self.schedule_apply_all()
        return geoip_list

    def _audit_list(self, action: AuditAction, geoip_list: GeoipList, **extra: Any) -> None:
        self.audit.record(
            action,
            AuditEntity.GEOIP,
            geoip_list.id,
            {
                "version": geoip_list.version,
                "count": geoip_list.prefixes_count,
                "sha256": geoip_list.sha256,
                "status": geoip_list.status.value,
                **extra,
            },
        )

    # --- исключения ----------------------------------------------------------------------------------------------

    async def add_override(self, data: OverrideCreate) -> GeoipOverride:
        try:
            network = validate_override(data.network, direct=data.action == OverrideAction.DIRECT)
        except GeoipError as exc:
            raise InvalidDataError(str(exc)) from None
        if data.action == OverrideAction.DIRECT:
            for protected in await self.protected_networks():
                if network.overlaps(protected):
                    raise InvalidDataError("A direct network must not cover the address of a gateway or an exit server")
        text = str(network)
        if await self.repository.get_override_by_network(text):
            raise StateConflictError("An override for this network already exists")
        override = await self.repository.add_override(GeoipOverride(network=text, action=data.action, note=data.note))
        self.audit.record(
            AuditAction.GEOIP_OVERRIDE_ADDED,
            AuditEntity.GEOIP,
            override.id,
            {"network": text, "action": data.action.value},
        )
        await self.session.commit()
        await self.schedule_apply_all()
        return override

    async def remove_override(self, override_id: UUID) -> None:
        override = await self.repository.get_override(override_id)
        if override is None:
            raise NotFoundError("Override not found")
        meta = {"network": override.network, "action": override.action.value}
        await self.repository.delete_override(override)
        self.audit.record(AuditAction.GEOIP_OVERRIDE_REMOVED, AuditEntity.GEOIP, override_id, meta)
        await self.session.commit()
        await self.schedule_apply_all()

    # --- применение на шлюзах ------------------------------------------------------------------------------------

    async def schedule_apply_all(self) -> list[Job]:
        """GATEWAY_APPLY_RU каждому серверу со шлюзом и SSH-учёткой (без дублей: пока по серверу есть незавершённая
        такая же, новая не ставится - она применит уже актуальный список). Активного списка нет - ничего."""
        if await self.repository.get_active() is None:
            return []
        queued: list[Job] = []
        job_repository, credentials = JobRepository(self.session), CredentialRepository(self.session)
        for protocol in await ProtocolRepository(self.session).list_by_type(ProtocolType.AMNEZIA_GATEWAY):
            if not await credentials.get_by_server_id(protocol.server_id):
                continue
            if await job_repository.has_unfinished_for_server(JobType.GATEWAY_APPLY_RU, protocol.server_id):
                continue
            queued.append(await self.jobs.create(JobType.GATEWAY_APPLY_RU, server_id=protocol.server_id))
        return queued
