"""Bot ownership checks for gateway device operations."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import GatewayApplyError, NotFoundError, ReasonConflictError, ServerBusyError, SSHError
from core.ssh_exception import SSHServerError
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import AccessGrantRepository, SubscriptionRepository
from modules.access.schemas import GatewayDeviceCreate, GatewayDevicesRead
from modules.access.service import VPNCredentialService
from modules.audit.actor import AuditActor
from modules.bot.schemas import DeviceExitChange, DeviceExitName, DeviceExitOption, DeviceExitOptions
from modules.gateway.device_selection import eligible_device, unavailable
from modules.gateway.failover import GatewayFailoverService
from modules.gateway.models.gateway_assignment import GatewayAssignment
from modules.gateway.repository import GatewayAssignmentRepository, GatewayExitRepository
from modules.jobs.models.job import Job
from modules.vpn_users.models.user import VPNUser
from modules.vpn_users.repository import VPNUserRepository


class BotDeviceService:
    def __init__(self, session: AsyncSession, actor: AuditActor):
        self.session = session
        self.actor = actor
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
        subscription = await SubscriptionRepository(self.session).get_by_id(grant.subscription_id) if grant else None
        if subscription is None or subscription.user_id != user.id or not credential.is_gateway:
            raise NotFoundError("Device not found")
        return credential

    async def revoke(self, telegram_id: int, credential_id: UUID) -> VPNCredential | Job:
        await self.owned(telegram_id, credential_id)
        return await self.credentials.schedule_revoke(credential_id)

    async def exit_options(self, telegram_id: int, credential_id: UUID) -> DeviceExitOptions:
        await self.owned(telegram_id, credential_id)
        user = await self.user(telegram_id)
        credential, gateway = await eligible_device(self.session, user.id, credential_id)
        assignment = await self.session.get(GatewayAssignment, credential_id, populate_existing=True)
        repo = GatewayExitRepository(self.session)
        counts = await GatewayAssignmentRepository(self.session).counts(gateway.id)
        exits = []
        names = {}
        for row, _, name in await repo.list_with_server_names(gateway.id):
            names[row.id] = DeviceExitName(id=row.id, name=name)
            reason = None
            if unavailable(row):
                reason = "unavailable"
            elif (
                row.max_clients is not None
                and counts.get(row.id, 0) >= row.max_clients
                and (assignment is None or assignment.exit_id != row.id)
            ):
                reason = "full"
            exits.append(DeviceExitOption(id=row.id, name=name, selectable=reason is None, reason=reason))
        return DeviceExitOptions(
            credential_id=credential.id,
            device_name=credential.device_name or "Устройство",
            current_exit=names.get(assignment.exit_id) if assignment else None,
            pinned_exit=names.get(assignment.pinned_exit_id) if assignment and assignment.pinned_exit_id else None,
            mode="pinned" if assignment and assignment.pinned_exit_id else "automatic",
            revision=assignment.revision if assignment else None,
            exits=exits,
        )

    async def change_exit(self, telegram_id: int, credential_id: UUID, data: DeviceExitChange) -> DeviceExitOptions:
        await self.owned(telegram_id, credential_id)
        user = await self.user(telegram_id)
        _, gateway = await eligible_device(self.session, user.id, credential_id)
        try:
            await GatewayFailoverService(self.session, self.actor).move_client(
                gateway.server_id,
                credential_id,
                data.exit_id,
                data.exit_id is not None,
                user_id=user.id,
                expected_revision=data.expected_revision,
            )
        except (SSHServerError, SSHError) as error:
            await self.session.rollback()
            raise GatewayApplyError() from error
        except ServerBusyError as error:
            raise ReasonConflictError("gateway_busy") from error
        return await self.exit_options(telegram_id, credential_id)
