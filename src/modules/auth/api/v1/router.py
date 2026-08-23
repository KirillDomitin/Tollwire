#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.postgres import get_session
from core.exceptions import InvalidCredentialsError
from modules.auth.dependencies import get_current_admin
from modules.auth.models.admin import Admin
from modules.auth.schemas import AdminRead, LoginRequest, TokenResponse
from modules.auth.service import AuthService

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def get_auth_service(session: Annotated[AsyncSession, Depends(get_session)]) -> AuthService:
    return AuthService(session)


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "refresh_token",
        token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=settings.jwt_refresh_expire,
        path="/api/v1/auth",
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, response: Response, service: AuthService = Depends(get_auth_service)
) -> TokenResponse:
    try:
        access, new_refresh = await service.login(str(body.email), body.password)
    except InvalidCredentialsError:
        raise HTTPException(401, detail="Invalid credentials")
    set_cookie(response, new_refresh)
    return TokenResponse(access_token=access)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> TokenResponse:
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        access, new_refresh = await service.refresh(refresh_token)
    except InvalidCredentialsError:
        raise HTTPException(401, detail="Invalid credentials")
    set_cookie(response, new_refresh)
    return TokenResponse(access_token=access)


@router.post("/logout")
async def logout(
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> dict[str, str]:
    if refresh_token:
        await service.logout(refresh_token)
    response.delete_cookie("refresh_token", path="/api/v1/auth")
    return {"detail": "logged out"}


@router.get("/me", response_model=AdminRead)
async def me(current_admin: Annotated[Admin, Depends(get_current_admin)]) -> Admin:
    return current_admin
