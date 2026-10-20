#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modules.geoip.models.geoip import GeoipList, GeoipOverride, GeoipStatus


class GeoipRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, list_id: UUID) -> GeoipList | None:
        return await self.session.get(GeoipList, list_id)

    async def get_active(self, *, lock: bool = False) -> GeoipList | None:
        """Активный список. lock=True - `FOR UPDATE`: два одновременных приёма списка идут по очереди."""
        query = select(GeoipList).where(GeoipList.status == GeoipStatus.ACTIVE)
        if lock:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_candidates(self) -> Sequence[GeoipList]:
        query = select(GeoipList).where(GeoipList.status == GeoipStatus.CANDIDATE).order_by(GeoipList.created_at.desc())
        return (await self.session.execute(query)).scalars().all()

    async def find_candidate_by_hash(self, sha256: str) -> GeoipList | None:
        query = select(GeoipList).where(GeoipList.status == GeoipStatus.CANDIDATE, GeoipList.sha256 == sha256)
        return (await self.session.execute(query)).scalars().first()

    async def supersede_candidates(self, except_id: UUID | None = None) -> None:
        """Устаревшие кандидаты: сохраняется только последний."""
        query = update(GeoipList).where(GeoipList.status == GeoipStatus.CANDIDATE).values(status=GeoipStatus.SUPERSEDED)
        if except_id is not None:
            query = query.where(GeoipList.id != except_id)
        await self.session.execute(query)

    async def add(self, geoip_list: GeoipList) -> GeoipList:
        self.session.add(geoip_list)
        await self.session.flush()
        return geoip_list

    async def list_overrides(self) -> Sequence[GeoipOverride]:
        query = select(GeoipOverride).order_by(GeoipOverride.created_at, GeoipOverride.network)
        return (await self.session.execute(query)).scalars().all()

    async def get_override(self, override_id: UUID) -> GeoipOverride | None:
        return await self.session.get(GeoipOverride, override_id)

    async def get_override_by_network(self, network: str) -> GeoipOverride | None:
        query = select(GeoipOverride).where(GeoipOverride.network == network)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def add_override(self, override: GeoipOverride) -> GeoipOverride:
        self.session.add(override)
        await self.session.flush()
        return override

    async def delete_override(self, override: GeoipOverride) -> None:
        await self.session.delete(override)
        await self.session.flush()
