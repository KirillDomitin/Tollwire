"""Слежение бота за панелью (`tollwire_bot.panel_watch`, этап 25.5, Р6): панель молчит дольше порога -> «Панель не
отвечает N мин» админам в Telegram, потом «Панель снова отвечает».

Время виртуальное (`WatchWorld.step`): цикл слежения идёт раз в 15 секунд, heartbeat - вручную. Главное:
- связь с панелью - только 2xx на heartbeat/claim; сеть, 5xx и `PanelUnavailable` - сбой; 401 и 429 - панель жива;
- повтор не чаще раза в 30 минут, «снова отвечает» - один раз после первого успеха;
- состояние (`last_ok_at`, момент сообщения, чаты, порог) переживает рестарт: дублей нет;
- чаты и порог приходят из ответа heartbeat; пустой список - только запись в лог;
- не больше одного сообщения в секунду в чат, 429 Telegram - пауза, 403 - чат выбывает до следующего heartbeat."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiogram.methods import SendMessage

from fakes import dropped, rate_limited, unauthorized, unavailable
from support import K, WatchWorld, retry_after_error, running_watch, t

CHAT = 5550001000
OTHER_CHAT = 5550002000
THRESHOLD = 300
MINUTE = 60


@pytest.fixture
async def watch(tmp_path: Path) -> AsyncIterator[WatchWorld]:
    async with running_watch(tmp_path) as running:
        yield running


def go_dark(world: WatchWorld, route: str = "POST /heartbeat") -> None:
    """Панель перестаёт отвечать надолго (сбои идут, пока тест не вызовет `recover`)."""
    world.panel.override(route, unavailable(), times=10_000)


# --- порог ----------------------------------------------------------------------------------------------------------


async def test_a_healthy_panel_never_triggers_a_message(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)

    await watch.step(30 * MINUTE, beat=True)

    assert watch.said(CHAT) == []


async def test_silence_shorter_than_the_threshold_says_nothing(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)

    await watch.step(THRESHOLD - 15)

    assert watch.said(CHAT) == []


async def test_silence_for_the_threshold_gives_one_message_with_the_minutes(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)

    await watch.step(THRESHOLD)

    assert watch.said(CHAT) == [t(K.panel_down, minutes=THRESHOLD // MINUTE)]


async def test_the_message_goes_to_every_chat_the_panel_named(watch: WatchWorld) -> None:
    await watch.configure([CHAT, OTHER_CHAT], THRESHOLD)

    await watch.step(THRESHOLD)

    assert len(watch.said(CHAT)) == 1 and len(watch.said(OTHER_CHAT)) == 1


async def test_the_message_is_plain_text(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)

    await watch.step(THRESHOLD)

    assert watch.telegram.parse_modes() == {None}


async def test_failed_heartbeats_count_as_silence(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    go_dark(watch)

    await watch.step(THRESHOLD + 2 * MINUTE, beat=True)

    assert len(watch.downs(CHAT)) == 1


@pytest.mark.parametrize("failure", [unavailable(503), unavailable(500), dropped()], ids=["503", "500", "dropped"])
async def test_server_errors_and_dropped_connections_are_an_outage(watch: WatchWorld, failure) -> None:  # type: ignore[no-untyped-def]
    await watch.configure([CHAT], THRESHOLD)
    watch.panel.override("POST /heartbeat", failure, times=10_000)

    await watch.step(THRESHOLD + 2 * MINUTE, beat=True)

    assert len(watch.downs(CHAT)) == 1


async def test_an_unauthorized_bot_is_not_a_dead_panel(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    watch.panel.override("POST /heartbeat", unauthorized(), times=10_000)

    await watch.step(20 * MINUTE, beat=True)

    assert watch.said(CHAT) == []


async def test_rate_limiting_is_not_a_dead_panel(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    watch.panel.override("POST /heartbeat", rate_limited(1), times=10_000)

    await watch.step(20 * MINUTE, beat=True)

    assert watch.said(CHAT) == []


async def test_a_successful_claim_also_proves_the_panel_is_alive(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    go_dark(watch)  # heartbeat не проходит, но очередь отвечает

    for _ in range(int(20 * MINUTE // 30)):
        await watch.step(30)
        await watch.client.claim(10)

    assert watch.said(CHAT) == []


# --- повтор и восстановление ----------------------------------------------------------------------------------------


async def test_the_message_repeats_no_sooner_than_thirty_minutes(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    await watch.step(THRESHOLD)
    assert len(watch.downs(CHAT)) == 1

    await watch.step(29 * MINUTE)
    assert len(watch.downs(CHAT)) == 1

    await watch.step(2 * MINUTE)
    assert len(watch.downs(CHAT)) == 2


async def test_recovery_is_announced_once(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    go_dark(watch)
    await watch.step(THRESHOLD + MINUTE)
    watch.panel.recover()

    await watch.step(MINUTE, beat=True)
    await watch.step(10 * MINUTE, beat=True)

    assert len(watch.ups(CHAT)) == 1 and watch.said(CHAT)[-1] == t(K.panel_up)


async def test_a_short_outage_ends_without_any_message(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    watch.panel.override("POST /heartbeat", unavailable(), times=3)

    await watch.step(5 * MINUTE - 30, beat=True)
    await watch.step(10 * MINUTE, beat=True)

    assert watch.said(CHAT) == []


async def test_a_new_outage_after_recovery_is_announced_again(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    await watch.step(THRESHOLD)
    await watch.heartbeat.beat()  # панель вернулась
    await watch.step(MINUTE)
    assert len(watch.ups(CHAT)) == 1

    await watch.step(THRESHOLD + MINUTE)

    assert len(watch.downs(CHAT)) == 2


# --- рестарт --------------------------------------------------------------------------------------------------------


async def test_a_restart_after_the_message_does_not_repeat_it(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    await watch.step(THRESHOLD)
    assert len(watch.downs(CHAT)) == 1

    await watch.restart()
    await watch.step(10 * MINUTE)

    assert len(watch.downs(CHAT)) == 1


async def test_recovery_after_a_restart_is_still_announced_to_the_same_chats(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    await watch.step(THRESHOLD)
    await watch.restart()  # чаты и порог бот помнит с диска: heartbeat после рестарта ещё не прошёл

    await watch.heartbeat.beat()
    await watch.step(MINUTE)

    assert len(watch.ups(CHAT)) == 1


async def test_the_outage_is_counted_from_the_last_success_not_from_the_restart(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    await watch.step(200)
    await watch.restart()

    await watch.step(105)  # 305 секунд с последнего успеха, но только 105 с рестарта

    assert len(watch.downs(CHAT)) == 1


async def test_a_restart_with_no_panel_after_the_threshold_sends_the_message_without_a_heartbeat(
    watch: WatchWorld,
) -> None:
    await watch.configure([CHAT], THRESHOLD)
    await watch.restart()
    go_dark(watch)

    await watch.step(THRESHOLD + MINUTE)

    assert len(watch.downs(CHAT)) == 1


async def test_on_an_empty_disk_with_a_dead_panel_the_watch_is_quiet_and_does_not_crash(tmp_path: Path) -> None:
    """Бот стартовал при упавшей панели: чатов он ещё не знает (их даёт только heartbeat), писать некому."""
    async with running_watch(tmp_path) as world:
        go_dark(world)

        await world.step(30 * MINUTE, beat=True)

        assert world.telegram.count(SendMessage) == 0


async def test_on_an_empty_disk_the_silence_is_counted_from_the_start(tmp_path: Path) -> None:
    """`last_ok_at` на пустом диске = момент старта: сообщение приходит через порог после старта, а не сразу."""
    async with running_watch(tmp_path) as world:
        world.panel.notify_chats, world.panel.notify_threshold = [CHAT], THRESHOLD
        await world.heartbeat.beat()  # чаты получены, успех в момент старта
        go_dark(world)

        await world.step(THRESHOLD - 15, beat=True)
        assert world.said(CHAT) == []
        await world.step(2 * MINUTE, beat=True)

        assert len(world.downs(CHAT)) == 1


# --- чаты и порог из heartbeat --------------------------------------------------------------------------------------


async def test_without_chats_nothing_is_sent_and_the_log_says_why(
    watch: WatchWorld, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await watch.configure([], THRESHOLD)

    await watch.step(THRESHOLD + MINUTE)

    assert watch.telegram.count(SendMessage) == 0
    assert any(record.name.endswith("panel_watch") for record in caplog.records)


async def test_an_old_panel_that_answers_with_nothing_gives_no_messages_and_no_crash(watch: WatchWorld) -> None:
    await watch.heartbeat.beat()  # ответ `{}`
    go_dark(watch)

    await watch.step(30 * MINUTE, beat=True)

    assert watch.telegram.count(SendMessage) == 0


async def test_a_broken_notify_block_keeps_the_previous_values(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    watch.panel.notify_raw = "garbage"
    await watch.heartbeat.beat()  # 2xx: панель жива, но настройки не разобрать

    watch.panel.override("POST /heartbeat", unavailable(), times=10_000)
    await watch.step(THRESHOLD + 2 * MINUTE, beat=True)

    assert len(watch.downs(CHAT)) == 1


async def test_a_new_threshold_applies_at_once(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    await watch.configure([CHAT], 600)

    await watch.step(THRESHOLD + MINUTE)
    assert watch.said(CHAT) == []
    await watch.step(THRESHOLD)

    assert len(watch.downs(CHAT)) == 1


async def test_a_chat_removed_by_the_panel_is_not_written_to(watch: WatchWorld) -> None:
    await watch.configure([CHAT, OTHER_CHAT], THRESHOLD)
    await watch.configure([CHAT], THRESHOLD)

    await watch.step(THRESHOLD)

    assert len(watch.said(CHAT)) == 1 and watch.said(OTHER_CHAT) == []


# --- Telegram -------------------------------------------------------------------------------------------------------


async def test_telegram_flood_control_pauses_the_sending(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    watch.telegram.fail(SendMessage, retry_after_error(7))

    await watch.step(THRESHOLD)

    assert len(watch.downs(CHAT)) == 1
    assert any(delay >= 7 for delay in watch.clock.sleeps)


async def test_a_blocked_chat_drops_out_and_the_others_still_get_messages(watch: WatchWorld) -> None:
    await watch.configure([CHAT, OTHER_CHAT], THRESHOLD)
    watch.telegram.forbidden.add(CHAT)

    await watch.step(THRESHOLD)
    await watch.step(31 * MINUTE)

    assert watch.said(CHAT) == []
    assert len(watch.downs(OTHER_CHAT)) == 2
    attempts = [m for m in watch.telegram.methods if isinstance(m, SendMessage) and int(str(m.chat_id)) == CHAT]
    assert len(attempts) == 1  # второй раз в закрывшийся чат не пишем


async def test_a_blocked_chat_returns_with_the_next_heartbeat(watch: WatchWorld) -> None:
    await watch.configure([CHAT], THRESHOLD)
    watch.telegram.forbidden.add(CHAT)
    await watch.step(THRESHOLD)
    watch.telegram.forbidden.discard(CHAT)  # человек разблокировал бота, админ привязал заново

    await watch.configure([CHAT], THRESHOLD)
    await watch.step(31 * MINUTE)

    assert len(watch.downs(CHAT)) == 1


async def test_many_chats_never_trip_the_telegram_limits(tmp_path: Path) -> None:
    async with running_watch(tmp_path, limits=True) as world:
        chats = [CHAT + number for number in range(25)]
        await world.configure(chats, THRESHOLD)

        await world.step(THRESHOLD + MINUTE)

        assert world.telegram.flood_errors == [] and all(len(world.downs(chat)) == 1 for chat in chats)
