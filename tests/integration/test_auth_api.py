"""Авторизация через настоящий HTTP: вход, refresh с ротацией, выход, отказ без/с плохим токеном, аудит входа."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import create_access_token
from modules.audit.models.audit_log import AuditAction, AuditLog
from tests.integration.conftest import AdminLogin

PROTECTED = "/api/v1/servers"


async def test_login_wrong_password_is_401_and_audited(
    client: httpx.AsyncClient, admin: AdminLogin, session: AsyncSession
) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": admin.email, "password": "wrong-" + admin.password}
    )
    assert response.status_code == 401
    (failed,) = (await session.execute(select(AuditLog).where(AuditLog.action == AuditAction.LOGIN_FAILED))).scalars()
    assert failed.meta == {"email": admin.email} and failed.ip_address == "127.0.0.1"
    assert admin.password not in str(failed.meta)


async def test_login_sets_httponly_refresh_cookie(client: httpx.AsyncClient, admin: AdminLogin) -> None:
    response = await client.post("/api/v1/auth/login", json={"email": admin.email, "password": admin.password})
    assert response.status_code == 200 and response.json()["access_token"]
    cookie = response.headers["set-cookie"].lower()
    assert "refresh_token=" in cookie and "httponly" in cookie and "path=/api/v1/auth" in cookie
    assert "samesite=lax" in cookie
    assert "refresh_token" not in response.json()  # refresh-токен — только в cookie, не в теле ответа


async def test_me_returns_current_admin(api: httpx.AsyncClient, admin: AdminLogin) -> None:
    response = await api.get("/api/v1/auth/me")
    assert response.status_code == 200 and response.json()["email"] == admin.email
    assert "password_hash" not in response.json()


async def test_protected_endpoints_reject_missing_and_bad_tokens(client: httpx.AsyncClient, admin: AdminLogin) -> None:
    assert (await client.get(PROTECTED)).status_code in (401, 403)
    assert (await client.get(PROTECTED, headers={"Authorization": "Bearer not.a.jwt"})).status_code == 401
    assert (await client.get("/api/v1/audit-logs")).status_code in (401, 403)
    stranger = create_access_token("00000000-0000-0000-0000-000000000000")  # подпись верная, админа нет
    assert (await client.get(PROTECTED, headers={"Authorization": f"Bearer {stranger}"})).status_code == 401


async def test_deactivated_admin_loses_access(api: httpx.AsyncClient, admin: AdminLogin, session: AsyncSession) -> None:
    assert (await api.get(PROTECTED)).status_code == 200
    admin.admin.is_active = False
    session.add(admin.admin)
    await session.commit()
    assert (await api.get(PROTECTED)).status_code == 401  # действующий токен больше не пускает


async def test_refresh_rotates_token(api: httpx.AsyncClient) -> None:
    old_refresh = api.cookies.get("refresh_token")
    assert old_refresh
    response = await api.post("/api/v1/auth/refresh")
    assert response.status_code == 200 and response.json()["access_token"]
    new_refresh = api.cookies.get("refresh_token")
    assert new_refresh and new_refresh != old_refresh
    api.cookies.clear()
    api.cookies.set("refresh_token", old_refresh)
    reuse = await api.post("/api/v1/auth/refresh")
    assert reuse.status_code == 401  # старый refresh одноразовый


async def test_logout_kills_refresh(api: httpx.AsyncClient, session: AsyncSession) -> None:
    refresh = api.cookies.get("refresh_token")
    assert (await api.post("/api/v1/auth/logout")).status_code == 200
    api.cookies.clear()
    api.cookies.set("refresh_token", refresh or "")
    assert (await api.post("/api/v1/auth/refresh")).status_code == 401
    api.cookies.clear()
    assert (await api.post("/api/v1/auth/refresh")).status_code == 401  # без cookie
    actions = {log.action for log in (await session.execute(select(AuditLog))).scalars()}
    assert {AuditAction.LOGIN, AuditAction.LOGOUT} <= actions


async def test_refresh_cookie_is_secure_in_production(
    client: httpx.AsyncClient, admin: AdminLogin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cookie_secure", True)  # как в docker-compose.prod.yml
    response = await client.post("/api/v1/auth/login", json={"email": admin.email, "password": admin.password})
    assert "; secure" in response.headers["set-cookie"].lower()  # браузер отправит cookie только по HTTPS
