"""Bot ownership checks for gateway device operations."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import AccessGrantRepository, SubscriptionRepository
from modules.access.schemas import GatewayDeviceCreate, GatewayDevicesRead
from modules.access.service import VPNCredentialService
from modules.audit.actor import AuditActor
from modules.jobs.models.job import Job
from modules.vpn_users.models.user import VPNUser
from modules.vpn_users.repository import VPNUserRepository


class BotDeviceService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.credentials = VPNCredentialService(session, actor)

    async def user(self, telegram_id: int) -> VPNUser:
        user = await VPNUserRepository(self.session).get_by_telegram_id(telegram_id)
        if user is None:
            raise NotFoundError("User not found")
        return user

    async def list(self, telegram_id: int) -> GatewayDevicesRead:
        user = await self.user(telegram_id)
        return await self.credentials.gateway_devices(user.id)

    async def add(self, telegram_id: int, data: GatewayDeviceCreate) -> Job:
        user = await self.user(telegram_id)
        return await self.credentials.add_gateway_device(user.id, data)

    async def owned(self, telegram_id: int, credential_id: UUID) -> VPNCredential:
        user = await self.user(telegram_id)
        credential = await self.credentials.get(credential_id)
        grant = await AccessGrantRepository(self.session).get_by_id(credential.access_grant_id)
        subscription = (
            await SubscriptionRepository(self.session).get_by_id(grant.subscription_id) if grant else None
        )
        if subscription is None or subscription.user_id != user.id or not credential.is_gateway:
            raise NotFoundError("Device not found")
        return credential

    async def revoke(self, telegram_id: int, credential_id: UUID) -> VPNCredential | Job:
        await self.owned(telegram_id, credential_id)
        return await self.credentials.schedule_revoke(credential_id)
