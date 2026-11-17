"""Authorization and eligibility shared by self-service reads and domain moves."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError, ReasonConflictError
from modules.access.models import CredentialState
from modules.access.models.vpn_credential import VPNCredential
from modules.access.repository import AccessGrantRepository, SubscriptionRepository
from modules.access.service import inactive_reason
from modules.gateway.balancer import UNUSABLE_HEALTH
from modules.gateway.models.gateway_exit import ExitState, GatewayExit
from modules.protocols.models.protocol import ProtocolType, VPNProtocol


async def eligible_device(
    session: AsyncSession, user_id: UUID, credential_id: UUID, *, lock: bool = False
) -> tuple[VPNCredential, VPNProtocol]:
    # Establish the chain before locking it in parent-to-child order. Always reread
    # after _ready, which can commit and release any earlier row locks.
    credential = await session.get(VPNCredential, credential_id, populate_existing=True)
    grants = AccessGrantRepository(session)
    grant = await grants.get_by_id(credential.access_grant_id) if credential else None
    if credential is None or grant is None:
        raise NotFoundError("Device not found")
    subscription = await SubscriptionRepository(session).get_by_id(grant.subscription_id, lock=lock)
    grant = await grants.get_by_id(grant.id, lock=lock)
    credential = await session.get(VPNCredential, credential_id, with_for_update=lock, populate_existing=True)
    if (
        subscription is None
        or grant is None
        or credential is None
        or subscription.user_id != user_id
        or not credential.is_gateway
        or credential.access_grant_id != grant.id
        or grant.subscription_id != subscription.id
    ):
        raise NotFoundError("Device not found")
    protocol = await session.get(VPNProtocol, grant.protocol_id, populate_existing=True)
    if protocol is None or protocol.type != ProtocolType.AMNEZIA_GATEWAY:
        raise NotFoundError("Device not found")
    if inactive_reason(grant, subscription, datetime.now(timezone.utc)) is not None:
        raise ReasonConflictError("no_access")
    if credential.state != CredentialState.ACTIVE or credential.revoked_at is not None:
        raise ReasonConflictError("not_ready")
    return credential, protocol


def unavailable(exit: GatewayExit) -> bool:
    return exit.state != ExitState.ACTIVE or exit.consecutive_failures > 0 or exit.health in UNUSABLE_HEALTH
