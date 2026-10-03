#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import AlreadyExistsError, InactiveAccessError, LiveCredentialExistsError, NotFoundError
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.schemas import (
    AccessGrantCreate,
    AccessGrantRead,
    SubscriptionCreate,
    SubscriptionRead,
    SubscriptionUpdate,
    VPNCredentialConfigRead,
    VPNCredentialRead,
)
from modules.access.service import AccessGrantService, SubscriptionService, VPNCredentialService
from modules.audit.actor import AuditActor
from modules.audit.dependencies import get_audit_actor
from modules.auth.dependencies import get_current_admin, require
from modules.auth.permissions import Permission
from modules.jobs.models.job import Job
from modules.jobs.schemas import JobRead

router = APIRouter(prefix="/api/v1", tags=["access"], dependencies=[Depends(get_current_admin)])


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[AuditActor, Depends(get_audit_actor)]


def get_subscription_service(session: SessionDep, actor: ActorDep) -> SubscriptionService:
    return SubscriptionService(session, actor)


def get_access_grant_service(session: SessionDep, actor: ActorDep) -> AccessGrantService:
    return AccessGrantService(session, actor)


def get_credential_service(session: SessionDep, actor: ActorDep) -> VPNCredentialService:
    return VPNCredentialService(session, actor)


@router.post(
    "/vpn-users/{user_id}/subscriptions",
    dependencies=[Depends(require(Permission.ACCESS_MANAGE))],
    response_model=SubscriptionRead,
    status_code=201,
)
async def create_subscription(
    user_id: UUID, body: SubscriptionCreate, service: SubscriptionService = Depends(get_subscription_service)
) -> Subscription:
    try:
        return await service.create(user_id, body)
    except NotFoundError:
        raise HTTPException(404, detail="VPN user not found")
    except AlreadyExistsError:
        raise HTTPException(409, detail="User already has an active subscription")


@router.get(
    "/vpn-users/{user_id}/subscriptions",
    dependencies=[Depends(require(Permission.ACCESS_READ))],
    response_model=list[SubscriptionRead],
)
async def list_subscriptions(
    user_id: UUID, service: SubscriptionService = Depends(get_subscription_service)
) -> Sequence[Subscription]:
    return await service.list_by_user(user_id)


@router.get(
    "/subscriptions/{subscription_id}",
    dependencies=[Depends(require(Permission.ACCESS_READ))],
    response_model=SubscriptionRead,
)
async def get_subscription(
    subscription_id: UUID, service: SubscriptionService = Depends(get_subscription_service)
) -> Subscription:
    try:
        return await service.get(subscription_id)
    except NotFoundError:
        raise HTTPException(404, detail="Subscription not found")


@router.patch(
    "/subscriptions/{subscription_id}",
    dependencies=[Depends(require(Permission.ACCESS_MANAGE))],
    response_model=SubscriptionRead,
)
async def update_subscription(
    subscription_id: UUID, body: SubscriptionUpdate, service: SubscriptionService = Depends(get_subscription_service)
) -> Subscription:
    try:
        return await service.update(subscription_id, body)
    except NotFoundError:
        raise HTTPException(404, detail="Subscription not found")


@router.delete(
    "/subscriptions/{subscription_id}", dependencies=[Depends(require(Permission.ACCESS_MANAGE))], status_code=204
)
async def delete_subscription(
    subscription_id: UUID, service: SubscriptionService = Depends(get_subscription_service)
) -> None:
    try:
        await service.delete(subscription_id)
    except NotFoundError:
        raise HTTPException(404, detail="Subscription not found")
    except LiveCredentialExistsError as exc:
        raise HTTPException(409, detail=str(exc))


@router.post(
    "/subscriptions/{subscription_id}/revoke",
    dependencies=[Depends(require(Permission.ACCESS_MANAGE))],
    response_model=SubscriptionRead,
)
async def revoke_subscription(
    subscription_id: UUID, service: SubscriptionService = Depends(get_subscription_service)
) -> Subscription:
    try:
        return await service.revoke(subscription_id)
    except NotFoundError:
        raise HTTPException(404, detail="Subscription not found")


@router.post(
    "/subscriptions/{subscription_id}/grants",
    dependencies=[Depends(require(Permission.ACCESS_MANAGE))],
    response_model=AccessGrantRead,
    status_code=201,
)
async def create_grant(
    subscription_id: UUID, body: AccessGrantCreate, service: AccessGrantService = Depends(get_access_grant_service)
) -> AccessGrant:
    try:
        return await service.create(subscription_id, body)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except AlreadyExistsError as exc:
        raise HTTPException(409, detail=str(exc))


@router.get(
    "/subscriptions/{subscription_id}/grants",
    dependencies=[Depends(require(Permission.ACCESS_READ))],
    response_model=list[AccessGrantRead],
)
async def list_grants(
    subscription_id: UUID, service: AccessGrantService = Depends(get_access_grant_service)
) -> Sequence[AccessGrant]:
    return await service.list_by_subscription(subscription_id)


@router.delete("/access/{access_grant_id}", dependencies=[Depends(require(Permission.ACCESS_MANAGE))], status_code=204)
async def delete_grant(access_grant_id: UUID, service: AccessGrantService = Depends(get_access_grant_service)) -> None:
    try:
        await service.delete(access_grant_id)
    except NotFoundError:
        raise HTTPException(404, detail="Access grant not found")
    except LiveCredentialExistsError as exc:
        raise HTTPException(409, detail=str(exc))


@router.post(
    "/access/{access_grant_id}/revoke",
    dependencies=[Depends(require(Permission.ACCESS_MANAGE))],
    response_model=AccessGrantRead,
)
async def revoke_grant(
    access_grant_id: UUID, service: AccessGrantService = Depends(get_access_grant_service)
) -> AccessGrant:
    try:
        return await service.revoke(access_grant_id)
    except NotFoundError:
        raise HTTPException(404, detail="Access grant not found")


@router.post(
    "/access/{access_grant_id}/credentials",
    dependencies=[Depends(require(Permission.ACCESS_MANAGE))],
    response_model=JobRead,
    status_code=202,
)
async def issue_credential(
    access_grant_id: UUID, service: VPNCredentialService = Depends(get_credential_service)
) -> Job:
    # Клиент создаётся на сервере воркером; результат — в GET /access/{id}/credentials после SUCCESS задачи.
    try:
        return await service.schedule_issue(access_grant_id)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except (InactiveAccessError, AlreadyExistsError) as exc:
        raise HTTPException(409, detail=str(exc))


@router.get(
    "/access/{access_grant_id}/credentials",
    dependencies=[Depends(require(Permission.ACCESS_READ))],
    response_model=list[VPNCredentialRead],
)
async def list_credentials(
    access_grant_id: UUID, service: VPNCredentialService = Depends(get_credential_service)
) -> Sequence[VPNCredential]:
    return await service.list_by_access_grant(access_grant_id)


@router.get(
    "/credentials/{credential_id}",
    dependencies=[Depends(require(Permission.ACCESS_READ))],
    response_model=VPNCredentialRead,
)
async def get_credential(
    credential_id: UUID, service: VPNCredentialService = Depends(get_credential_service)
) -> VPNCredential:
    try:
        return await service.get(credential_id)
    except NotFoundError:
        raise HTTPException(404, detail="Credential not found")


@router.post(
    "/credentials/{credential_id}/config",
    dependencies=[Depends(require(Permission.CONFIG_REVEAL))],
    response_model=VPNCredentialConfigRead,
)
async def reveal_credential_config(
    credential_id: UUID, response: Response, service: VPNCredentialService = Depends(get_credential_service)
) -> VPNCredentialConfigRead:
    # POST, а не GET: это действие с последствиями (будущий аудит), и такие ответы не кэшируются.
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.reveal_config(credential_id)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except InactiveAccessError as exc:
        raise HTTPException(409, detail=str(exc))


@router.post(
    "/credentials/{credential_id}/revoke",
    dependencies=[Depends(require(Permission.ACCESS_MANAGE))],
    response_model=JobRead | VPNCredentialRead,
    status_code=202,
    responses={200: {"model": VPNCredentialRead, "description": "Credential is already revoked"}},
)
async def revoke_credential(
    credential_id: UUID, response: Response, service: VPNCredentialService = Depends(get_credential_service)
) -> JobRead | VPNCredentialRead:
    # 202 + задача, если клиента ещё надо убрать с сервера; 200 + credential, если он уже отозван.
    try:
        result = await service.schedule_revoke(credential_id)
    except NotFoundError:
        raise HTTPException(404, detail="Credential not found")
    if isinstance(result, VPNCredential):
        response.status_code = 200
        return VPNCredentialRead.model_validate(result)
    return JobRead.model_validate(result)
