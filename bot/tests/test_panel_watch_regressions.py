"""A live HTTP error suppresses alarms without declaring an existing incident recovered."""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fakes import Outcome, rate_limited, unauthorized
from support import K, running_watch, t
from tollwire_bot.pacing import MessagePacer
from tollwire_bot.panel import NotifyConfig


@pytest.mark.parametrize("response", [unauthorized(), rate_limited(1)], ids=["401", "429"])
async def test_live_errors_preserve_incident_until_2xx_even_across_restart(tmp_path: Path, response: Outcome) -> None:
    chat = 5550001000
    async with running_watch(tmp_path) as world:
        await world.configure([chat], 300)
        await world.step(300)
        assert len(world.downs(chat)) == 1

        world.panel.override("POST /heartbeat", response)
        await world.heartbeat.beat()
        await world.restart()
        await world.step(60)
        assert world.said(chat) == [t(K.panel_down, minutes=5)]

        await world.heartbeat.beat()
        await world.step(15)
        await world.restart()
        await world.step(15)
        assert world.said(chat) == [t(K.panel_down, minutes=5), t(K.panel_up)]


@pytest.mark.parametrize("response", [unauthorized(), rate_limited(1)], ids=["401", "429"])
async def test_live_error_during_down_send_does_not_schedule_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: Outcome
) -> None:
    chat = 5550001000
    async with running_watch(tmp_path) as world:
        await world.configure([chat], 300)
        original_send = world.bot.send_message

        async def send_with_live_error(*args: Any, **kwargs: Any) -> Any:
            result = await original_send(*args, **kwargs)
            world.panel.override("POST /heartbeat", response)
            await world.heartbeat.beat()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(world.bot, "send_message", send_with_live_error)
            await world.step(300)
        await world.step(15)
        await world.restart()
        await world.step(15)
        assert world.said(chat) == [t(K.panel_down, minutes=5)]

        await world.heartbeat.beat()
        await world.step(15)
        assert world.ups(chat) == [t(K.panel_up)]


class ControlledPacer(MessagePacer):
    def __init__(self) -> None:
        super().__init__(20)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def acquire(self, chat: int) -> None:
        self.entered.set()
        await self.release.wait()


@pytest.mark.parametrize("change", ["removed_down", "recovered_down", "removed_up"])
async def test_watch_rechecks_delivery_after_waiting_for_shared_pacer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    chat = 5550001000
    async with running_watch(tmp_path) as world:
        await world.configure([chat], 300)
        if change == "removed_up":
            await world.step(300)
            await world.heartbeat.beat()
        else:
            world.clock.advance(300)
        before = list(world.said(chat))
        pacer = ControlledPacer()
        monkeypatch.setattr(world.watch, "_pacer", pacer)
        delivery = asyncio.create_task(world.watch.tick())
        try:
            await asyncio.wait_for(pacer.entered.wait(), timeout=5)
            if change == "recovered_down":
                await world.client.claim(10)
            else:
                await world.configure([], 300)
        finally:
            pacer.release.set()
            await delivery
        assert world.said(chat) == before


async def test_cancelled_persist_finishes_worker_before_shutdown_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_watch(tmp_path) as world:
        await world.configure([5550001000], 300)
        original_run = world.storage._run
        entered, release = threading.Event(), threading.Event()
        first = True

        async def controlled_run(work: Callable[..., Any]) -> Any:
            nonlocal first
            if not first:
                return await original_run(work)
            first = False

            def blocked_worker(connection: Any) -> Any:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("the test did not release the SQLite worker")
                return work(connection)

            return await original_run(blocked_worker)

        monkeypatch.setattr(world.storage, "_run", controlled_run)
        stale = asyncio.create_task(world.watch.persist())
        shutdown: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            stale.cancel()
            await asyncio.sleep(0)
            # A repeated cancellation must not release ownership either.
            stale.cancel()
            world.watch.configure(NotifyConfig(600, [5550001000]))
            shutdown = asyncio.create_task(world.watch.persist())
            await asyncio.sleep(0.01)
            assert not stale.done() and not shutdown.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await stale
            if shutdown is not None:
                await shutdown

        state = await world.storage.panel_watch_load()
        assert state is not None and state["threshold"] == 600
        await world.restart()
        await world.step(300)
        assert world.said(5550001000) == []
