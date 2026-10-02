#!/usr/bin/python
# -*- coding: utf-8 -*-
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import get_session
from core.exceptions import AlreadyExistsError, InactiveAccessError, NotFoundError
from modules.access.models.access_grant import AccessGrant
from modules.access.models.subscription import Subscription
from modules.access.models.vpn_credential import VPNCredential
from modules.access.schemas import (
    AccessGrantCreate,
    AccessGrantRead,
    AccessGrantUpdate,
    SubscriptionCreate,
    SubscriptionRead,
    SubscriptionUpdate,
    VPNCredentialConfigRead,
    VPNCredentialRead,
)
from modules.access.service import AccessGrantService, SubscriptionService, VPNCredentialService
from modules.auth.dependencies import get_current_admin

router = APIRouter(prefix="/api/v1", tags=["access"], dependencies=[Depends(get_current_admin)])


def get_subscription_service(session: Annotated[AsyncSession, Depends(get_session)]) -> SubscriptionService:
    return SubscriptionService(session)


def get_access_grant_service(session: Annotated[AsyncSession, Depends(get_session)]) -> AccessGrantService:
    return AccessGrantService(session)


def get_credential_service(session: Annotated[AsyncSession, Depends(get_session)]) -> VPNCredentialService:
    return VPNCredentialService(session)


@router.post("/vpn-users/{user_id}/subscriptions", response_model=SubscriptionRead, status_code=201)
async def create_subscription(
    user_id: UUID, body: SubscriptionCreate, service: SubscriptionService = Depends(get_subscription_service)
) -> Subscription:
    try:
        return await service.create(user_id, body)
    except NotFoundError:
        raise HTTPException(404, detail="VPN user not found")
    except AlreadyExistsError:
        raise HTTPException(409, detail="User already has an active subscription")


@router.get("/vpn-users/{user_id}/subscriptions", response_model=list[SubscriptionRead])
async def list_subscriptions(
    user_id: UUID, service: SubscriptionService = Depends(get_subscription_service)
) -> Sequence[Subscription]:
    return await service.list_by_user(user_id)


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionRead)
async def get_subscription(
    subscription_id: UUID, service: SubscriptionService = Depends(get_subscription_service)
) -> Subscription:
    try:
        return await service.get(subscription_id)
    except NotFoundError:
        raise HTTPException(404, detail="Subscription not found")


@router.patch("/subscriptions/{subscription_id}", response_model=SubscriptionRead)
async def update_subscription(
    subscription_id: UUID, body: SubscriptionUpdate, service: SubscriptionService = Depends(get_subscription_service)
) -> Subscription:
    try:
        return await service.update(subscription_id, body)
    except NotFoundError:
        raise HTTPException(404, detail="Subscription not found")


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def delete_subscription(
    subscription_id: UUID, service: SubscriptionService = Depends(get_subscription_service)
) -> None:
    try:
        await service.delete(subscription_id)
    except NotFoundError:
        raise HTTPException(404, detail="Subscription not found")


@router.post("/subscriptions/{subscription_id}/revoke", response_model=SubscriptionRead)
async def revoke_subscription(
    subscription_id: UUID, service: SubscriptionService = Depends(get_subscription_service)
) -> Subscription:
    try:
        return await service.revoke(subscription_id)
    except NotFoundError:
        raise HTTPException(404, detail="Subscription not found")


@router.post("/subscriptions/{subscription_id}/grants", response_model=AccessGrantRead, status_code=201)
async def create_grant(
    subscription_id: UUID, body: AccessGrantCreate, service: AccessGrantService = Depends(get_access_grant_service)
) -> AccessGrant:
    try:
        return await service.create(subscription_id, body)
    except NotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except AlreadyExistsError as exc:
        raise HTTPException(409, detail=str(exc))


@router.get("/subscriptions/{subscription_id}/grants", response_model=list[AccessGrantRead])
async def list_grants(
    subscription_id: UUID, service: AccessGrantService = Depends(get_access_grant_service)
) -> Sequence[AccessGrant]:
    return await service.list_by_subscription(subscription_id)


@router.patch("/access/{access_grant_id}", response_model=AccessGrantRead)
async def update_grant(
    access_grant_id: UUID, body: AccessGrantUpdate, service: AccessGrantService = Depends(get_access_grant_service)
) -> AccessGrant:
    try:
        return await service.update(access_grant_id, body)
    except NotFoundError:
        raise HTTPException(404, detail="Access grant not found")


@router.delete("/access/{access_grant_id}", status_code=204)
async def delete_grant(access_grant_id: UUID, service: AccessGrantService = Depends(get_access_grant_service)) -> None:
    try:
        await service.delete(access_grant_id)
    except NotFoundError:
        raise HTTPException(404, detail="Access grant not found")


@router.post("/access/{access_grant_id}/revoke", response_model=AccessGrantRead)
async def revoke_grant(
    access_grant_id: UUID, service: AccessGrantService = Depends(get_access_grant_service)
) -> AccessGrant:
    try:
        return await service.revoke(access_grant_id)
    except NotFoundError:
        raise HTTPException(404, detail="Access grant not found")


@router.get("/access/{access_grant_id}/credentials", response_model=list[VPNCredentialRead])
async def list_credentials(
    access_grant_id: UUID, service: VPNCredentialService = Depends(get_credential_service)
) -> Sequence[VPNCredential]:
    return await service.list_by_access_grant(access_grant_id)


@router.get("/credentials/{credential_id}", response_model=VPNCredentialRead)
async def get_credential(
    credential_id: UUID, service: VPNCredentialService = Depends(get_credential_service)
) -> VPNCredential:
    try:
        return await service.get(credential_id)
    except NotFoundError:
        raise HTTPException(404, detail="Credential not found")


@router.post("/credentials/{credential_id}/config", response_model=VPNCredentialConfigRead)
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


@router.post("/credentials/{credential_id}/revoke", response_model=VPNCredentialRead)
async def revoke_credential(
    credential_id: UUID, service: VPNCredentialService = Depends(get_credential_service)
) -> VPNCredential:
    try:
        return await service.revoke(credential_id)
    except NotFoundError:
        raise HTTPException(404, detail="Credential not found")
