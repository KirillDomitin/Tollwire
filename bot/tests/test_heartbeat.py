"""Heartbeat: getMe один раз при старте, затем POST /heartbeat каждые 30 с; сбои пишутся в лог только при смене
состояния (ok <-> down), чтобы остановленная панель не заливала журнал строкой раз в полминуты."""

import asyncio
import contextlib
import logging

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe

from fakes import BOT_USERNAME, Outcome, dropped, rate_limited, unauthorized, unavailable
from support import World, make_heartbeat, wait_until
from tollwire_bot import __version__

HEARTBEAT_LOGGER = "tollwire_bot.heartbeat"


def heartbeat_records(caplog: pytest.LogCaptureFixture, level: int = logging.INFO) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == HEARTBEAT_LOGGER and r.levelno >= level]


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)


async def test_get_me_is_asked_once_and_the_name_goes_to_the_panel(world: World) -> None:
    beat = make_heartbeat(world)

    await beat.start()
    await beat.beat()
    await beat.beat()
    await beat.beat()

    assert world.telegram.count(GetMe) == 1
    assert [h["bot_username"] for h in world.panel.heartbeats] == [BOT_USERNAME] * 3


async def test_heartbeat_reports_the_bot_version(world: World) -> None:
    beat = make_heartbeat(world)
    await beat.start()

    await beat.beat()

    assert world.panel.heartbeats[0]["version"] == __version__
    assert world.panel.calls[-1].headers["user-agent"] == f"tollwire-bot/{__version__}"


async def test_the_loop_beats_every_thirty_seconds(world: World) -> None:
    beat = make_heartbeat(world)
    await beat.start()
    loop = asyncio.create_task(beat.run())
    try:
        await wait_until(lambda: len(world.panel.heartbeats) >= 4, what="four heartbeats")
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop

    times = [h["at"] for h in world.panel.heartbeats[:4]]
    assert [later - earlier for earlier, later in zip(times, times[1:])] == [30, 30, 30]


async def test_a_wrong_token_stops_the_bot_before_it_reports_anything(world: World) -> None:
    world.telegram.fail(GetMe, TelegramUnauthorizedError(method=GetMe(), message="Unauthorized"))
    beat = make_heartbeat(world)

    with pytest.raises(TelegramUnauthorizedError):
        await beat.start()

    assert world.panel.heartbeats == []


# --- сбои ------------------------------------------------------------------------------------------------------------


async def test_an_outage_is_logged_once_and_the_recovery_once(world: World, caplog: pytest.LogCaptureFixture) -> None:
    beat = make_heartbeat(world)
    await beat.start()
    await beat.beat()
    assert heartbeat_records(caplog, logging.WARNING) == []  # пока всё хорошо, журнал молчит

    world.panel.override("POST /heartbeat", unavailable(), times=100)
    for _ in range(3):
        await beat.beat()
    assert len(heartbeat_records(caplog, logging.WARNING)) == 1  # три сбоя подряд - одна строка

    world.panel.recover()
    await beat.beat()
    await beat.beat()
    await beat.beat()

    assert len(heartbeat_records(caplog)) == 2  # «упала» и «снова на связи», больше ничего


async def test_a_bot_that_starts_against_a_dead_panel_logs_that_once(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    beat = make_heartbeat(world)
    await beat.start()
    world.panel.override("POST /heartbeat", dropped(), times=100)

    for _ in range(4):
        await beat.beat()

    assert len(heartbeat_records(caplog, logging.WARNING)) == 1


@pytest.mark.parametrize("outcome", [unavailable(), dropped(), rate_limited(60), unauthorized()])
async def test_a_failed_beat_does_not_stop_the_loop(world: World, outcome: Outcome) -> None:
    beat = make_heartbeat(world)
    await beat.start()
    world.panel.override("POST /heartbeat", outcome, times=100)

    await beat.beat()  # не бросает: бот живёт и дальше, панель может вернуться

    world.panel.recover()
    await beat.beat()
    assert len(world.panel.heartbeats) == 1


async def test_an_outage_log_has_no_key_or_token(world: World, caplog: pytest.LogCaptureFixture) -> None:
    beat = make_heartbeat(world)
    await beat.start()
    world.panel.override("POST /heartbeat", unavailable(), times=100)

    await beat.beat()

    assert world.panel.key not in caplog.text
    assert world.bot.token not in caplog.text


async def test_cancelling_the_loop_is_quiet(world: World, caplog: pytest.LogCaptureFixture) -> None:
    beat = make_heartbeat(world)
    await beat.start()
    loop = asyncio.create_task(beat.run())
    await wait_until(lambda: len(world.panel.heartbeats) >= 1, what="the first heartbeat")

    loop.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop

    assert heartbeat_records(caplog, logging.WARNING) == []
