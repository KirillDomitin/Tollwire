from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.auth.models.admin import Admin
from modules.bot.models.admin_link import AdminTelegramLink


class AdminLinkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_admin(self, admin_id: UUID) -> Admin | None:
        result = await self.session.execute(
            select(Admin).where(Admin.id == admin_id).with_for_update().execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get(self, admin_id: UUID) -> AdminTelegramLink | None:
        result = await self.session.execute(
            select(AdminTelegramLink)
            .where(AdminTelegramLink.admin_id == admin_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def admin_for_code(self, code_hash: str) -> UUID | None:
        result = await self.session.execute(
            select(AdminTelegramLink.admin_id).where(AdminTelegramLink.code_hash == code_hash)
        )
        return result.scalar_one_or_none()

    async def telegram_owner(self, telegram_id: int) -> UUID | None:
        result = await self.session.execute(
            select(AdminTelegramLink.admin_id).where(AdminTelegramLink.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def live_links(self) -> Sequence[AdminTelegramLink]:
        return (
            await self.session.scalars(
                select(AdminTelegramLink)
                .join(Admin, Admin.id == AdminTelegramLink.admin_id)
                .where(
                    Admin.is_active.is_(True),
                    AdminTelegramLink.telegram_id.is_not(None),
                    AdminTelegramLink.blocked_at.is_(None),
                )
                .order_by(AdminTelegramLink.created_at, AdminTelegramLink.id)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def read(self, admin_id: UUID) -> AdminTelegramLink | None:
        result = await self.session.execute(
            select(AdminTelegramLink)
            .where(AdminTelegramLink.admin_id == admin_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def recipients(self) -> Sequence[tuple[AdminTelegramLink, str]]:
        result = await self.session.execute(
            select(AdminTelegramLink, Admin.email)
            .join(Admin, Admin.id == AdminTelegramLink.admin_id)
            .where(Admin.is_active.is_(True), AdminTelegramLink.telegram_id.is_not(None))
            .order_by(Admin.email, AdminTelegramLink.id)
        )
        return result.tuples().all()

    async def count_linked(self) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(AdminTelegramLink)
            .join(Admin, Admin.id == AdminTelegramLink.admin_id)
            .where(Admin.is_active.is_(True), AdminTelegramLink.telegram_id.is_not(None))
        )
        return int(result.scalar_one())
