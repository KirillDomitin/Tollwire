"""Журнал аудита по HTTP: курсорная пагинация, фильтр по нескольким действиям, email админа, список авторов."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.auth.models.admin import AdminRole
from tests.integration.conftest import AdminLogin, login_client, make_admin

URL = "/api/v1/audit-logs"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


async def add_logs(
    session: AsyncSession,
    count: int,
    *,
    created_at: datetime = T0,
    action: AuditAction = AuditAction.SERVER_UPDATED,
    **fields: Any,
) -> list[UUID]:
    logs = [AuditLog(action=action, created_at=created_at, **fields) for _ in range(count)]
    session.add_all(logs)
    await session.commit()
    return [log.id for log in logs]


def cursor(row: dict[str, Any]) -> dict[str, str]:
    return {"before_created_at": row["created_at"], "before_id": row["id"]}


# --- курсор ----------------------------------------------------------------------------------------------------------


async def test_cursor_walks_batch_with_identical_created_at_each_row_exactly_once(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    ids = set(map(str, await add_logs(session, 5)))  # одно и то же время — различает только id
    seen: list[str] = []
    params: dict[str, Any] = {"limit": 2, "action": "server_updated"}
    for _ in range(10):
        page = (await api.get(URL, params=params)).json()
        seen += [row["id"] for row in page]
        if len(page) < 2:
            break
        params = {"limit": 2, "action": "server_updated", **cursor(page[-1])}
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))


async def test_cursor_orders_by_created_at_then_id_descending(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_logs(session, 3, created_at=T0)
    await add_logs(session, 2, created_at=T0 + timedelta(minutes=1))
    rows = (await api.get(URL, params={"action": "server_updated"})).json()
    keys = [(row["created_at"], row["id"]) for row in rows]
    assert len(keys) == 5 and keys == sorted(keys, reverse=True)


async def test_new_record_between_pages_does_not_duplicate_a_row(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_logs(session, 4)
    first = (await api.get(URL, params={"limit": 2, "action": "server_updated"})).json()
    await add_logs(session, 1, created_at=T0 + timedelta(hours=1))  # новее всего — попадает «наверх», сдвигая offset
    second = (await api.get(URL, params={"limit": 2, "action": "server_updated", **cursor(first[-1])})).json()
    first_ids, second_ids = {row["id"] for row in first}, {row["id"] for row in second}
    assert len(second) == 2 and not first_ids & second_ids


async def test_cursor_past_the_last_row_is_empty(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_logs(session, 2)
    rows = (await api.get(URL, params={"action": "server_updated"})).json()
    assert (await api.get(URL, params={"action": "server_updated", **cursor(rows[-1])})).json() == []


async def test_before_created_at_without_before_id_is_422(api: httpx.AsyncClient) -> None:
    response = await api.get(URL, params={"before_created_at": T0.isoformat()})
    assert response.status_code == 422


async def test_before_id_without_before_created_at_is_422(api: httpx.AsyncClient) -> None:
    assert (await api.get(URL, params={"before_id": str(uuid4())})).status_code == 422


async def test_malformed_cursor_is_422(api: httpx.AsyncClient) -> None:
    response = await api.get(URL, params={"before_created_at": T0.isoformat(), "before_id": "not-a-uuid"})
    assert response.status_code == 422


# --- фильтр по действиям ---------------------------------------------------------------------------------------------


async def test_action_filter_accepts_several_actions(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_logs(session, 1, action=AuditAction.SERVER_UPDATED)
    await add_logs(session, 2, action=AuditAction.SERVER_DELETED)
    await add_logs(session, 1, action=AuditAction.SERVER_CREATED)
    rows = (await api.get(URL, params={"action": ["server_updated", "server_deleted"], "limit": 100})).json()
    assert sorted(row["action"] for row in rows) == ["server_deleted", "server_deleted", "server_updated"]


async def test_single_action_filter_still_works(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_logs(session, 1, action=AuditAction.SERVER_UPDATED)
    await add_logs(session, 1, action=AuditAction.SERVER_DELETED)
    rows = (await api.get(URL, params={"action": "server_deleted"})).json()
    assert [row["action"] for row in rows] == ["server_deleted"]


async def test_unknown_action_in_list_is_422(api: httpx.AsyncClient) -> None:
    assert (await api.get(URL, params={"action": ["server_updated", "nope"]})).status_code == 422


async def test_multi_action_filter_combines_with_cursor(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_logs(session, 3, action=AuditAction.SERVER_UPDATED)
    await add_logs(session, 3, action=AuditAction.SERVER_DELETED)
    await add_logs(session, 2, action=AuditAction.SERVER_CREATED)
    params: dict[str, Any] = {"action": ["server_updated", "server_deleted"], "limit": 4}
    first = (await api.get(URL, params=params)).json()
    second = (await api.get(URL, params={**params, **cursor(first[-1])})).json()
    assert len(first) == 4 and len(second) == 2
    assert {row["action"] for row in first + second} == {"server_updated", "server_deleted"}


# --- email админа ----------------------------------------------------------------------------------------------------


async def test_admin_email_is_filled_for_viewer(session: AsyncSession, admin: AdminLogin) -> None:
    await add_logs(session, 1, action=AuditAction.SERVER_CREATED, admin_id=admin.admin.id)
    viewer = await make_admin(session, AdminRole.VIEWER)
    async with login_client(viewer) as http:
        rows = (await http.get(URL, params={"action": "server_created"})).json()
    assert [(row["admin_id"], row["admin_email"]) for row in rows] == [(str(admin.admin.id), admin.email)]


async def test_admin_email_is_none_without_admin(api: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_logs(
        session, 1, action=AuditAction.LOGIN_FAILED, meta={"email": "ghost@example.com"}, ip_address="203.0.113.5"
    )
    (row,) = (await api.get(URL, params={"action": "login_failed"})).json()
    assert row["admin_id"] is None and row["admin_email"] is None
    assert row["metadata"]["email"] == "ghost@example.com"


async def test_admin_email_survives_cursor_pages(
    api: httpx.AsyncClient, session: AsyncSession, admin: AdminLogin
) -> None:
    await add_logs(session, 3, admin_id=admin.admin.id)
    first = (await api.get(URL, params={"limit": 2, "action": "server_updated"})).json()
    second = (await api.get(URL, params={"limit": 2, "action": "server_updated", **cursor(first[-1])})).json()
    assert {row["admin_email"] for row in first + second} == {admin.email}


# --- список авторов --------------------------------------------------------------------------------------------------


async def test_actors_lists_only_admins_present_in_the_log(
    api: httpx.AsyncClient, session: AsyncSession, admin: AdminLogin
) -> None:
    quiet = await make_admin(session, AdminRole.OPERATOR)  # в журнале не встречается
    await add_logs(session, 2, admin_id=admin.admin.id)
    await add_logs(session, 1, action=AuditAction.LOGIN_FAILED, meta={"email": "x@example.com"})
    actors = (await api.get(f"{URL}/actors")).json()
    assert actors == [{"id": str(admin.admin.id), "email": admin.email}]
    assert quiet.email not in str(actors)


async def test_actors_open_to_viewer(session: AsyncSession, admin: AdminLogin) -> None:
    await add_logs(session, 1, admin_id=admin.admin.id)
    async with login_client(await make_admin(session, AdminRole.VIEWER)) as http:
        response = await http.get(f"{URL}/actors")
    assert response.status_code == 200
    assert admin.email in [actor["email"] for actor in response.json()]


async def test_actors_requires_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"{URL}/actors")).status_code == 401


async def test_actors_expose_only_id_and_email(
    api: httpx.AsyncClient, session: AsyncSession, admin: AdminLogin
) -> None:
    await add_logs(session, 1, admin_id=admin.admin.id)
    (actor,) = (await api.get(f"{URL}/actors")).json()
    assert set(actor) == {"id", "email"}
