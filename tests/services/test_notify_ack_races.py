"""Notify acknowledgements across relinks and concurrent unlink transactions."""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from modules.audit.actor import SYSTEM_ACTOR
from modules.bot.admin_link_repository import AdminLinkRepository
from modules.bot.admin_links import AdminLinkService
from modules.bot.models.admin_link import AdminTelegramLink
from modules.bot.outbox import OutboxService
from tests.bot_notify_support import (
    TG_ADMIN,
    TG_ADMIN_2,
    admin_login,
    bind,
    claim_notify,
    issue_code,
    link_of,
    make_link,
    queue_notify,
)
from tests.bot_support import ack, ack_item, create_key, outbox_row, sql, status_value


@pytest.mark.parametrize("telegram_id", [TG_ADMIN, TG_ADMIN_2])
async def test_old_blocked_ack_does_not_block_a_relinked_admin(session: AsyncSession, telegram_id: int) -> None:
    admin = await admin_login()
    link_id = await make_link(admin.admin.id)
    key, _ = await create_key(session)
    row_id = await queue_notify(link_id)
    claimed = await claim_notify(session, key)
    code, _ = await issue_code(session, admin.admin.id)
    await bind(session, code, telegram_id)

    result = await ack(session, claimed.lease_id, ack_item(row_id, "blocked"))

    assert result.applied == [row_id]
    assert status_value((await outbox_row(row_id)).status) == "blocked"
    link = await link_of(admin.admin.id)
    assert link is not None and link["telegram_id"] == telegram_id and link["blocked_at"] is None
    await queue_notify(link_id)
    assert (await claim_notify(session, key)).items[0].telegram_id == telegram_id


async def test_new_lease_after_relink_can_block_the_current_link(session: AsyncSession) -> None:
    admin = await admin_login()
    link_id = await make_link(admin.admin.id)
    key, _ = await create_key(session)
    row_id = await queue_notify(link_id)
    old_claim = await claim_notify(session, key)
    code, _ = await issue_code(session, admin.admin.id)
    await bind(session, code, TG_ADMIN_2)
    await sql("UPDATE bot_outbox SET claimed_until = now() - interval '1 second' WHERE id = :id", id=row_id)
    claimed = await claim_notify(session, key)
    assert claimed.items[0].telegram_id == TG_ADMIN_2

    stale = await ack(session, old_claim.lease_id, ack_item(row_id, "blocked"))
    assert stale.ignored == [row_id]
    await ack(session, claimed.lease_id, ack_item(row_id, "blocked"))
    link = await link_of(admin.admin.id)
    assert link is not None and link["blocked_at"] is not None


async def test_claim_stamps_lease_when_transaction_predates_link(session: AsyncSession) -> None:
    admin = await admin_login()
    link_id = await make_link(admin.admin.id)
    key, _ = await create_key(session)
    row_id = await queue_notify(link_id)
    await session.execute(text("SELECT 1"))  # Fix PostgreSQL now() before the relink.
    async with session_factory() as other:
        service = AdminLinkService(other, SYSTEM_ACTOR)
        code, _ = await service.issue_code(admin.admin.id)
        await service.link(code, TG_ADMIN_2)
    before_claim = datetime.now(timezone.utc)

    claimed = await claim_notify(session, key)

    assert (await outbox_row(row_id)).updated_at >= before_claim
    await ack(session, claimed.lease_id, ack_item(row_id, "blocked"))
    link = await link_of(admin.admin.id)
    assert link is not None and link["blocked_at"] is not None


@pytest.mark.parametrize("operation", ["ack", "claim"])
async def test_unlink_does_not_deadlock_with_outbox(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    admin = await admin_login()
    link_id = await make_link(admin.admin.id)
    key, _ = await create_key(session)
    row_id = await queue_notify(link_id)
    claimed = await claim_notify(session, key) if operation == "ack" else None
    link_locked = asyncio.Event()
    release_unlink = asyncio.Event()
    worker_started = asyncio.Event()
    worker_pid: list[int] = []
    original_get = AdminLinkRepository.get

    async def paused_get(repository: AdminLinkRepository, admin_id: UUID) -> AdminTelegramLink | None:
        link = await original_get(repository, admin_id)
        link_locked.set()
        await release_unlink.wait()
        return link

    monkeypatch.setattr(AdminLinkRepository, "get", paused_get)

    async def unlink() -> None:
        async with session_factory() as other:
            await AdminLinkService(other, SYSTEM_ACTOR).unlink(admin.admin.id)

    async def worker():  # type: ignore[no-untyped-def]
        async with session_factory() as other:
            pid = await other.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            worker_pid.append(pid)
            worker_started.set()
            service = OutboxService(other, SYSTEM_ACTOR)
            if claimed is not None:
                return await service.ack(claimed.lease_id, [ack_item(row_id, "blocked")])
            return await service.claim(key.id, 50, kinds=["notify"])

    async def wait_until_worker_is_blocked() -> None:
        async with session_factory() as observer:
            while not await observer.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": worker_pid[0]}
            ):
                await asyncio.sleep(0.01)

    unlink_task = asyncio.create_task(unlink())
    worker_task = None
    try:
        await asyncio.wait_for(link_locked.wait(), timeout=5)
        worker_task = asyncio.create_task(worker())
        await asyncio.wait_for(worker_started.wait(), timeout=5)
        if operation == "ack":
            # Prove overlap through PostgreSQL's lock graph, not an arbitrary sleep.
            await asyncio.wait_for(wait_until_worker_is_blocked(), timeout=5)
        else:
            result = await asyncio.wait_for(asyncio.shield(worker_task), timeout=5)
            assert [item.id for item in result.items] == [row_id]
        release_unlink.set()
        await asyncio.wait_for(asyncio.shield(unlink_task), timeout=5)
        result = await asyncio.wait_for(asyncio.shield(worker_task), timeout=5)
        if operation == "ack":
            assert result.applied == [] and result.ignored == [row_id]
    finally:
        release_unlink.set()
        tasks = [unlink_task, *([worker_task] if worker_task is not None else [])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert await link_of(admin.admin.id) is None
