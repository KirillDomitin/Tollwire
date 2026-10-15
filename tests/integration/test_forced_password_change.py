"""Принудительная смена пароля (этап 20.2): пока стоит must_change_password, панелью пользоваться нельзя — открыты
только /auth/me, смена пароля, выход и обновление токена."""

import re
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from main import app
from modules.auth.models.admin import AdminRole
from tests.integration.conftest import AdminLogin, login_client, make_admin
from tests.integration.test_rbac import API_ROUTES

DETAIL = "Password change required"
NEW_PASSWORD = "a-brand-new-passphrase-42"


async def temporary_admin(session: AsyncSession, role: AdminRole = AdminRole.SUPERADMIN) -> AdminLogin:
    temporary = await make_admin(session, role)
    temporary.admin.must_change_password = True
    session.add(temporary.admin)
    await session.commit()
    return temporary


@pytest.mark.parametrize("role", list(AdminRole))
async def test_every_endpoint_is_closed_until_password_is_changed(session: AsyncSession, role: AdminRole) -> None:
    """Обход всех маршрутов: ни один не отвечает по существу. Новый маршрут без общей проверки уронит тест."""
    async with login_client(await temporary_admin(session, role)) as http:
        open_routes: list[str] = []
        for method, path, _ in API_ROUTES:
            url = re.sub(r"\{[a-z_]+\}", str(uuid.uuid4()), path)
            response = await http.request(method, url, json={})
            if response.status_code != 403 or response.json().get("detail") != DETAIL:
                open_routes.append(f"{method} {path} → {response.status_code}")
        assert open_routes == []


async def test_me_logout_and_refresh_stay_open_for_temporary_password(session: AsyncSession) -> None:
    async with login_client(await temporary_admin(session)) as http:
        me = await http.get("/api/v1/auth/me")
        assert (
            me.status_code == 200 and me.json()["must_change_password"] is True
        )  # по нему интерфейс покажет экран смены
        assert (await http.post("/api/v1/auth/refresh")).status_code == 200  # сессия не умирает, пока пароль не сменён
        assert (await http.post("/api/v1/auth/logout")).status_code == 200


async def test_access_opens_after_password_change(session: AsyncSession) -> None:
    temporary = await temporary_admin(session)
    async with login_client(temporary) as http:
        assert (await http.get("/api/v1/servers")).status_code == 403
        response = await http.post(
            "/api/v1/auth/change-password",
            json={"current_password": temporary.password, "new_password": NEW_PASSWORD},
        )
        assert response.status_code == 200
        http.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        assert (await http.get("/api/v1/servers")).status_code == 200
        assert (await http.get("/api/v1/auth/me")).json()["must_change_password"] is False


async def test_flag_set_while_logged_in_applies_immediately(
    api: httpx.AsyncClient, session: AsyncSession, admin: AdminLogin
) -> None:
    """Флаг читается из БД на каждом запросе: уже выданный access-токен после его установки пускать нельзя."""
    assert (await api.get("/api/v1/servers")).status_code == 200
    admin.admin.must_change_password = True
    session.add(admin.admin)
    await session.commit()
    assert (await api.get("/api/v1/servers")).status_code == 403


async def test_new_admin_must_change_password_by_default(api: httpx.AsyncClient) -> None:
    body = {"email": "new@example.com", "password": "long-enough-pass", "role": "viewer"}
    created = await api.post("/api/v1/admins", json=body)
    assert created.status_code == 201 and created.json()["must_change_password"] is True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as other:
        login = await other.post("/api/v1/auth/login", json={"email": body["email"], "password": body["password"]})
        assert login.status_code == 200
        other.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        assert (await other.get("/api/v1/servers")).status_code == 403


async def test_creator_can_waive_the_temporary_password(api: httpx.AsyncClient) -> None:
    body = {
        "email": "service@example.com",
        "password": "long-enough-pass",
        "role": "viewer",
        "must_change_password": False,
    }
    created = await api.post("/api/v1/admins", json=body)
    assert created.status_code == 201 and created.json()["must_change_password"] is False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as other:
        login = await other.post("/api/v1/auth/login", json={"email": body["email"], "password": body["password"]})
        other.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        assert (await other.get("/api/v1/servers")).status_code == 200
