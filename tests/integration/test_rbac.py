"""RBAC: у каждого эндпоинта есть право; матрица «роль × эндпоинт»; управление админами и его предохранители."""

import asyncio
import re
import uuid
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from core.exceptions import AdminRuleError
from main import app
from modules.audit.actor import AuditActor
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.auth.admin_service import AdminService
from modules.auth.models.admin import Admin, AdminRole
from modules.auth.permissions import Permission, has_permission
from modules.auth.schemas import AdminUpdate
from tests.integration.conftest import AdminLogin, login_client, make_admin

PUBLIC = {
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/auth/me",
    "/api/v1/auth/change-password",
    "/health",
}


def required_permissions(route: APIRoute) -> list[Permission]:
    found: list[Permission] = []

    def walk(dependant: Any) -> None:
        permission = getattr(dependant.call, "required_permission", None)
        if permission is not None:
            found.append(permission)
        for sub in dependant.dependencies:
            walk(sub)

    walk(route.dependant)
    return found


def api_routes(routes: list[Any]) -> list[APIRoute]:
    """Все APIRoute приложения. FastAPI 0.141 хранит подключённые роутеры обёрткой (_IncludedRouter), а не
    раскладывает их маршруты в app.routes, — заходим внутрь (пути в original_router уже с префиксом)."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found.extend(api_routes(route.original_router.routes))
    return found


API_ROUTES = [
    (method, route.path, route)
    for route in api_routes(app.routes)
    if route.path not in PUBLIC
    for method in sorted(route.methods)
]


def test_every_endpoint_requires_exactly_one_permission() -> None:
    """Новый эндпоинт без require(...) открыл бы действие любой роли — этот тест его поймает."""
    assert len(API_ROUTES) >= 40
    missing = [f"{m} {p}" for m, p, route in API_ROUTES if len(required_permissions(route)) != 1]
    assert missing == []


@pytest.mark.parametrize("role", list(AdminRole))
async def test_role_matrix_over_all_endpoints(role: AdminRole, session: AsyncSession) -> None:
    """Каждый эндпоинт под каждой ролью: 403 тогда и только тогда, когда у роли нет права.
    id случайные — разрешённый запрос получает 404/409/422, но ничего не создаёт и не меняет."""
    async with login_client(await make_admin(session, role)) as http:
        wrong: list[str] = []
        for method, path, route in API_ROUTES:
            (permission,) = required_permissions(route)
            url = re.sub(r"\{[a-z_]+\}", str(uuid.uuid4()), path)
            response = await http.request(method, url, json={})
            denied = response.status_code == 403
            if denied == has_permission(role, permission):
                wrong.append(f"{method} {path} → {response.status_code} ({permission.value})")
        assert wrong == []


def test_matrix_is_what_was_agreed() -> None:
    viewer, operator = AdminRole.VIEWER, AdminRole.OPERATOR
    assert not has_permission(viewer, Permission.CONFIG_REVEAL)  # приватный ключ клиента — не для «только чтение»
    assert has_permission(viewer, Permission.AUDIT_READ)
    assert has_permission(operator, Permission.CONFIG_REVEAL) and has_permission(operator, Permission.SERVERS_CHECK)
    assert not has_permission(operator, Permission.SERVERS_MANAGE)
    assert not has_permission(operator, Permission.ADMINS_MANAGE)
    assert all(has_permission(AdminRole.SUPERADMIN, p) for p in Permission)


async def test_me_shows_role_and_permissions(session: AsyncSession) -> None:
    async with login_client(await make_admin(session, AdminRole.OPERATOR)) as http:
        me = (await http.get("/api/v1/auth/me")).json()
    assert me["role"] == "operator" and me["is_superuser"] is False
    assert "config:reveal" in me["permissions"] and "servers:manage" not in me["permissions"]
    assert me["last_login_at"] is not None  # вход записал время


# --- управление админами ---------------------------------------------------------------------------------------------


async def test_superadmin_manages_admins_and_changes_apply_immediately(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    body = {
        "email": "op@example.com",
        "password": "long-enough-pass",
        "role": "operator",
        "must_change_password": False,
    }
    created = await api.post("/api/v1/admins", json=body)
    assert created.status_code == 201 and created.json()["role"] == "operator"
    assert "password_hash" not in created.text and body["password"] not in created.text  # ни пароля, ни хэша
    assert (await api.post("/api/v1/admins", json=body)).status_code == 409
    assert (await api.post("/api/v1/admins", json={"email": "x@example.com", "password": "short"})).status_code == 422
    op_id = uuid.UUID(created.json()["id"])
    operator = await session.get(Admin, op_id)
    assert operator is not None

    async with login_client(AdminLogin(operator, body["email"], body["password"])) as op:
        new_user = {"username": "u1", "email": "u1@example.com"}
        assert (await op.post("/api/v1/vpn-users", json=new_user)).status_code == 201
        refresh_cookie = op.cookies.get("refresh_token")

        # понизили до viewer — тот же access-токен сразу теряет право, refresh-сессия завершена
        demoted = await api.patch(f"/api/v1/admins/{op_id}", json={"role": "viewer"})
        assert demoted.status_code == 200 and demoted.json()["role"] == "viewer"
        other_user = {"username": "u2", "email": "u2@example.com"}
        assert (await op.post("/api/v1/vpn-users", json=other_user)).status_code == 403
        assert (await op.get("/api/v1/vpn-users")).status_code == 200  # читать по-прежнему можно
        op.cookies.set("refresh_token", refresh_cookie or "")
        assert (await op.post("/api/v1/auth/refresh")).status_code == 401

        # отключили — access-токен больше не пускает вообще
        assert (await api.patch(f"/api/v1/admins/{op_id}", json={"is_active": False})).status_code == 200
        assert (await op.get("/api/v1/vpn-users")).status_code == 401

    logs = list((await session.execute(select(AuditLog).where(AuditLog.entity_id == op_id))).scalars())
    actions = [log.action for log in logs]
    assert actions.count(AuditAction.ADMIN_CREATED) == 1 and actions.count(AuditAction.ADMIN_UPDATED) == 2
    demote = next(log for log in logs if log.action == AuditAction.ADMIN_UPDATED and "role" in log.meta["changes"])
    assert demote.meta["changes"]["role"] == ["operator", "viewer"] and demote.meta["sessions_revoked"] == 1


async def test_cannot_change_yourself(api: httpx.AsyncClient, admin: AdminLogin) -> None:
    for body in ({"role": "viewer"}, {"is_active": False}):
        response = await api.patch(f"/api/v1/admins/{admin.admin.id}", json=body)
        assert response.status_code == 409 and "yourself" in response.json()["detail"]
    assert (await api.patch(f"/api/v1/admins/{uuid.uuid4()}", json={"role": "viewer"})).status_code == 404


async def test_deactivated_admin_cannot_refresh(session: AsyncSession) -> None:
    viewer = await make_admin(session, AdminRole.VIEWER)
    async with login_client(viewer) as http:
        viewer.admin.is_active = False  # отключили в обход API (сессии не завершены) — refresh всё равно не продлит
        session.add(viewer.admin)
        await session.commit()
        assert (await http.post("/api/v1/auth/refresh")).status_code == 401


async def test_two_superadmins_cannot_demote_each_other_at_once(session: AsyncSession) -> None:
    """Последний активный superadmin защищён и от гонки: два встречных понижения — проходит только одно."""
    a, b = await make_admin(session), await make_admin(session)

    async def demote(actor: AdminLogin, target: AdminLogin) -> str:
        async with session_factory() as s:
            try:
                await AdminService(s, AuditActor(admin_id=actor.admin.id)).update(
                    target.admin.id, AdminUpdate(role=AdminRole.OPERATOR)
                )
                return "ok"
            except AdminRuleError:
                return "refused"

    results = await asyncio.gather(demote(a, b), demote(b, a))
    assert sorted(results) == ["ok", "refused"]
    supers = (await session.execute(select(Admin).where(Admin.role == AdminRole.SUPERADMIN))).scalars().all()
    assert len(supers) == 1


CRITICAL = {
    "POST /api/v1/credentials/{credential_id}/config": Permission.CONFIG_REVEAL,
    "PUT /api/v1/servers/{server_id}/credentials": Permission.SERVERS_MANAGE,
    "POST /api/v1/servers/{server_id}/test-connection": Permission.SERVERS_MANAGE,
    "POST /api/v1/servers/{server_id}/install": Permission.SERVERS_MANAGE,
    "POST /api/v1/servers/{server_id}/ssh-key": Permission.SERVERS_MANAGE,
    "PUT /api/v1/servers/{server_id}/host-key": Permission.SERVERS_MANAGE,
    "DELETE /api/v1/servers/{server_id}/host-key": Permission.SERVERS_MANAGE,
    "DELETE /api/v1/servers/{server_id}": Permission.SERVERS_MANAGE,
    "POST /api/v1/servers/{server_id}/sync": Permission.SERVERS_CHECK,
    "POST /api/v1/servers/{server_id}/discover": Permission.SERVERS_CHECK,  # как health: запуск проверки
    "GET /api/v1/servers/{server_id}/discovery": Permission.SERVERS_READ,
    "POST /api/v1/servers/{server_id}/import": Permission.SERVERS_MANAGE,  # пишет в БД панели: как установка
    "GET /api/v1/servers/{server_id}/gateway/exits": Permission.SERVERS_READ,
    "GET /api/v1/servers/{server_id}/gateway/assignments": Permission.SERVERS_READ,
    "POST /api/v1/servers/{server_id}/gateway/exits": Permission.SERVERS_MANAGE,  # ставит клиента на выход и туннель
    "PATCH /api/v1/servers/{server_id}/gateway/exits/{exit_id}": Permission.SERVERS_MANAGE,
    "DELETE /api/v1/servers/{server_id}/gateway/exits/{exit_id}": Permission.SERVERS_MANAGE,
    "POST /api/v1/servers/{server_id}/gateway/ru/apply": Permission.SERVERS_MANAGE,  # меняет маршруты шлюза
    "GET /api/v1/geoip": Permission.SERVERS_READ,
    "GET /api/v1/geoip/overrides": Permission.SERVERS_READ,
    "POST /api/v1/geoip/update": Permission.SERVERS_MANAGE,  # ходит на внешний источник и перестраивает шлюзы
    "POST /api/v1/geoip/upload": Permission.SERVERS_MANAGE,
    "POST /api/v1/geoip/{list_id}/activate": Permission.SERVERS_MANAGE,
    "POST /api/v1/geoip/overrides": Permission.SERVERS_MANAGE,
    "DELETE /api/v1/geoip/overrides/{override_id}": Permission.SERVERS_MANAGE,
    "POST /api/v1/admins": Permission.ADMINS_MANAGE,
    "PATCH /api/v1/admins/{admin_id}": Permission.ADMINS_MANAGE,
    "GET /api/v1/audit-logs": Permission.AUDIT_READ,
    "GET /api/v1/audit-logs/actors": Permission.AUDIT_READ,
    "POST /api/v1/access/{access_grant_id}/credentials": Permission.ACCESS_MANAGE,
}


def test_critical_endpoints_keep_their_permission() -> None:
    """Матрица выше проверяет, что право соблюдается; здесь — что у опасных действий ПРАВИЛЬНОЕ право.
    Поменять — осознанно, вместе с этим списком."""
    declared = {f"{m} {p}": required_permissions(route)[0] for m, p, route in API_ROUTES}
    assert {key: declared.get(key) for key in CRITICAL} == CRITICAL
