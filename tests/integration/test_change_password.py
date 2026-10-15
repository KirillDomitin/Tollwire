"""Смена пароля админа (этап 20.1): проверка текущего пароля, отзыв прежних сессий и access-токенов, лимит попыток
подбора, флаг must_change_password, аудит без паролей."""

from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from modules.audit.models.audit_log import AuditAction, AuditLog
from tests.integration.conftest import AdminLogin, login_client, make_admin

URL = "/api/v1/auth/change-password"
NEW_PASSWORD = "a-brand-new-passphrase-42"


def body(admin: AdminLogin, new: str = NEW_PASSWORD, current: str | None = None) -> dict[str, str]:
    return {"current_password": current or admin.password, "new_password": new}


async def login(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def test_change_password_switches_credentials(
    api: httpx.AsyncClient, client: httpx.AsyncClient, admin: AdminLogin
) -> None:
    response = await api.post(URL, json=body(admin))
    assert response.status_code == 200 and response.json()["access_token"]
    assert "refresh_token=" in response.headers["set-cookie"]  # текущая вкладка остаётся в системе
    assert (await login(client, admin.email, admin.password)).status_code == 401  # старый пароль не работает
    assert (await login(client, admin.email, NEW_PASSWORD)).status_code == 200


async def test_current_session_keeps_working_after_change(api: httpx.AsyncClient, admin: AdminLogin) -> None:
    response = await api.post(URL, json=body(admin))
    api.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    assert (await api.get("/api/v1/auth/me")).status_code == 200  # новый токен не отсекается проверкой iat
    assert (await api.post("/api/v1/auth/refresh")).status_code == 200  # и новый refresh-токен рабочий


async def test_other_sessions_and_old_access_tokens_are_revoked(
    api: httpx.AsyncClient, session: AsyncSession, admin: AdminLogin
) -> None:
    """Пароль меняют, когда подозревают, что учётку взяли: чужой refresh и ещё живой access должны умереть."""
    async with login_client(admin) as stolen:
        stolen_refresh = stolen.cookies.get("refresh_token")
        stolen_access = jwt.encode(
            {
                "sub": str(admin.admin.id),
                "type": "access",
                "iat": datetime.now(timezone.utc) - timedelta(minutes=5),
                "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
            },
            settings.jwt_secret_key.get_secret_value(),
            algorithm="HS256",
        )
        assert (
            await stolen.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {stolen_access}"})
        ).status_code == 200

        assert (await api.post(URL, json=body(admin))).status_code == 200

        stolen.cookies.clear()
        stolen.cookies.set("refresh_token", stolen_refresh or "")
        assert (await stolen.post("/api/v1/auth/refresh")).status_code == 401
        assert (
            await stolen.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {stolen_access}"})
        ).status_code == 401


async def test_wrong_current_password_changes_nothing(
    api: httpx.AsyncClient, client: httpx.AsyncClient, admin: AdminLogin
) -> None:
    response = await api.post(URL, json=body(admin, current="not-the-password-123"))
    assert response.status_code == 400  # не 401: клиент принял бы это за конец сессии
    assert (await login(client, admin.email, admin.password)).status_code == 200  # пароль прежний
    assert (await login(client, admin.email, NEW_PASSWORD)).status_code == 401


async def test_stolen_access_token_cannot_guess_current_password(api: httpx.AsyncClient, admin: AdminLogin) -> None:
    """С украденным access-токеном смена пароля — оракул для подбора текущего: нужен тот же порог, что у входа."""
    for _ in range(settings.login_max_failures):
        assert (await api.post(URL, json=body(admin, current="guess-guess-guess"))).status_code == 400
    locked = await api.post(URL, json=body(admin))  # даже верный пароль теперь не принимается
    assert locked.status_code == 429 and "retry-after" in locked.headers


async def test_successful_change_resets_failure_counter(api: httpx.AsyncClient, admin: AdminLogin) -> None:
    attempts = settings.login_max_failures - 1
    for _ in range(attempts):
        await api.post(URL, json=body(admin, current="guess-guess-guess"))
    response = await api.post(URL, json=body(admin))
    assert response.status_code == 200
    api.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    # Прошлые промахи не копятся: без сброса эти попытки вместе с прежними упёрлись бы в порог (429).
    for _ in range(attempts):
        wrong = await api.post(URL, json={"current_password": "guess-guess-guess", "new_password": NEW_PASSWORD + "!"})
        assert wrong.status_code == 400
    back = await api.post(URL, json={"current_password": NEW_PASSWORD, "new_password": admin.password})
    assert back.status_code == 200


@pytest.mark.parametrize("new", ["short", "x" * 257, ""])
async def test_new_password_length_is_validated(api: httpx.AsyncClient, admin: AdminLogin, new: str) -> None:
    assert (await api.post(URL, json=body(admin, new=new))).status_code == 422


async def test_new_password_must_differ_from_current(api: httpx.AsyncClient, admin: AdminLogin) -> None:
    assert (await api.post(URL, json=body(admin, new=admin.password))).status_code == 422


async def test_must_change_flag_is_exposed_and_cleared(session: AsyncSession, client: httpx.AsyncClient) -> None:
    temporary = await make_admin(session)
    temporary.admin.must_change_password = True
    session.add(temporary.admin)
    await session.commit()
    async with login_client(temporary) as http:
        assert (await http.get("/api/v1/auth/me")).json()["must_change_password"] is True
        response = await http.post(URL, json=body(temporary))
        assert response.status_code == 200
        http.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        assert (await http.get("/api/v1/auth/me")).json()["must_change_password"] is False


async def test_change_requires_authentication(client: httpx.AsyncClient, admin: AdminLogin) -> None:
    assert (await client.post(URL, json=body(admin))).status_code in (401, 403)


async def test_change_is_audited_without_passwords(
    api: httpx.AsyncClient, session: AsyncSession, admin: AdminLogin
) -> None:
    await api.post(URL, json=body(admin, current="not-the-password-123"))
    await api.post(URL, json=body(admin))
    (changed,) = (
        await session.execute(select(AuditLog).where(AuditLog.action == AuditAction.PASSWORD_CHANGED))
    ).scalars()
    assert changed.admin_id == admin.admin.id and changed.entity_id == admin.admin.id
    (failed,) = (
        await session.execute(select(AuditLog).where(AuditLog.action == AuditAction.PASSWORD_CHANGE_FAILED))
    ).scalars()
    assert failed.admin_id == admin.admin.id
    everything = str([(log.meta, log.action) for log in (await session.execute(select(AuditLog))).scalars()])
    assert admin.password not in everything and NEW_PASSWORD not in everything and "not-the-password" not in everything
