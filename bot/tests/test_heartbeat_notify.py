"""Heartbeat бота 0.3.0 (этап 25.5, Р6): `local` в запросе, `NotifyConfig` (чаты и порог «панель недоступна») в ответе,
передача настроек слежению. Старая панель отвечает `{}` - бот живёт дальше."""

from typing import Any

from support import World, make_heartbeat
from tollwire_bot import __version__

CHAT = 5550001000


class RecordingWatch:
    """Слежение-пустышка: что бы heartbeat ни передал, запоминает."""

    def __init__(self) -> None:
        self.configs: list[Any] = []

    def configure(self, config: Any) -> None:
        self.configs.append(config)

    def on_ok(self) -> None: ...

    def on_fail(self) -> None: ...


def client_for(world: World, url: str) -> Any:
    from tollwire_bot.panel import PanelClient

    return PanelClient(url, world.panel.key, transport=world.panel.transport, sleep=world.clock.sleep)


async def test_the_client_returns_the_chats_and_the_threshold(world: World) -> None:
    world.panel.notify_chats, world.panel.notify_threshold = [CHAT, CHAT + 1], 420

    config = await world.client.heartbeat("tollwire_test_bot", __version__)

    assert config.chats == [CHAT, CHAT + 1] and config.panel_down_after_seconds == 420


async def test_an_old_panel_with_an_empty_answer_gives_an_empty_config(world: World) -> None:
    config = await world.client.heartbeat("tollwire_test_bot", __version__)

    assert config.chats == []


async def test_garbage_in_the_notify_block_does_not_break_the_heartbeat(world: World) -> None:
    for raw in ("garbage", [], {"chats": "x"}, {"chats": ["x", CHAT], "panel_down_after_seconds": "soon"}):
        world.panel.notify_raw = raw

        config = await world.client.heartbeat("tollwire_test_bot", __version__)

        assert all(isinstance(chat, int) for chat in config.chats)


async def test_the_beat_passes_the_config_to_the_watch(world: World) -> None:
    from tollwire_bot.heartbeat import Heartbeat

    world.panel.notify_chats, world.panel.notify_threshold = [CHAT], 600
    watch = RecordingWatch()
    beat = Heartbeat(world.bot, world.client, version=__version__, sleep=world.clock.sleep, watch=watch)  # type: ignore[call-arg]
    await beat.start()

    await beat.beat()

    (config,) = watch.configs
    assert config.chats == [CHAT] and config.panel_down_after_seconds == 600


async def test_a_failed_beat_does_not_reconfigure_the_watch(world: World) -> None:
    from fakes import unavailable
    from tollwire_bot.heartbeat import Heartbeat

    watch = RecordingWatch()
    beat = Heartbeat(world.bot, world.client, version=__version__, sleep=world.clock.sleep, watch=watch)  # type: ignore[call-arg]
    await beat.start()
    world.panel.override("POST /heartbeat", unavailable(), times=20)

    await beat.beat()

    assert watch.configs == []


async def test_the_request_says_the_panel_is_remote_for_an_ordinary_address(world: World) -> None:
    beat = make_heartbeat(world)
    await beat.start()

    await beat.beat()

    assert world.panel.heartbeats[-1]["local"] is False


async def test_the_request_says_the_panel_is_local_for_an_internal_name(world: World) -> None:
    from tollwire_bot.heartbeat import Heartbeat

    client = client_for(world, "http://api:8000")
    try:
        beat = Heartbeat(world.bot, client, version=__version__, sleep=world.clock.sleep)
        await beat.start()

        await beat.beat()
    finally:
        await client.aclose()

    assert world.panel.heartbeats[-1]["local"] is True
