#!/usr/bin/python
# -*- coding: utf-8 -*-
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditEntity
from modules.audit.service import AuditService
from modules.bot.models.settings import BotSettings
from modules.bot.repository import BotSettingsRepository
from modules.bot.schemas import BotSettingsUpdate
from modules.protocols.repository import ProtocolRepository


class BotSettingsService:
    """Настройки бота: протокол и сроки по умолчанию для приглашений. Читаются при каждом обращении."""

    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.settings = BotSettingsRepository(session)
        self.protocols = ProtocolRepository(session)
        self.audit = AuditService(session, actor)
        self.actor = actor

    async def get(self) -> BotSettings:
        row = await self.settings.get_or_create()
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def update(self, data: BotSettingsUpdate) -> BotSettings:
        if data.default_protocol_id is not None and not await self.protocols.get_by_id(data.default_protocol_id):
            raise NotFoundError(f"Protocol '{data.default_protocol_id}' not found")
        row = await self.settings.get_or_create()
        new_values = data.model_dump()
        changes = {
            field: [getattr(row, field), value] for field, value in new_values.items() if getattr(row, field) != value
        }
        for field, value in new_values.items():
            setattr(row, field, value)
        row.updated_by = self.actor.admin_id
        if changes:
            self.audit.record(AuditAction.BOT_SETTINGS_UPDATED, AuditEntity.BOT_SETTINGS, None, {"changes": changes})
        await self.session.commit()
        await self.session.refresh(row)  # updated_at выставляет БД: без refresh атрибут «просрочен», async не прочтёт
        return row
