"""Отправитель очереди (`tollwire_bot.outbox.OutboxSender`, этап 25.4): забирает сообщения у панели, отправляет их в
Telegram сам держа лимиты, подтверждает итог.

Время виртуальное (FakeClock), Telegram - FakeTelegram с лимитами Bot API (больше 30 сообщений в секунду или больше
одного в секунду в один чат - 429), панель - FakePanel с исходящей очередью. Главное:
- рассылка на 1000 человек идёт без единого 429, не плотнее OUTBOX_RATE_PER_SECOND в любом окне в секунду, около 50 с;
- 429 останавливает отправителя целиком на `retry_after`, 403 помечает «заблокировал», 400 - ошибка, сеть - повтор;
- аренда: за 5 секунд до её конца бот не отправляет, остаток возвращает как `retry`;
- журнал: сообщение, которое Telegram принял, не уходит второй раз, даже если подтверждение потерялось
  (кроме ссылки на конфиг: каждая выдача несёт новую ссылку, её отправляют всегда);
- ни текста новости, ни адреса ссылки нет ни в логе, ни в журнале, ни в том, что бот сообщает панели."""

import io
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import SendMessage
from pydantic import ValidationError

from fakes import OUTBOX_ACK, OUTBOX_CLAIM, rate_limited, unauthorized, unavailable
from support import (
    SenderWorld,
    config_message,
    make_settings,
    retry_after_error,
    running_sender,
    setup_logging,
    telegram_error,
    wait_until,
)

CHAT = 5550001000
EDGE = 1e-6
NEWS = "<b>Плановые работы</b> сегодня в 23:00"


@pytest.fixture
async def sender(tmp_path: Path) -> AsyncIterator[SenderWorld]:
    async with running_sender(tmp_path) as running:
        yield running


def chats(count: int, first: int = CHAT) -> list[int]:
    return [first + number for number in range(count)]


def queue_news_for(world: SenderWorld, ids: list[int], text: str = NEWS) -> None:
    broadcast = "e1e1e1e1-e1e1-41e1-81e1-e1e1e1e1e1e1"
    for chat in ids:
        world.panel.queue_news(chat, text, broadcast)


async def all_done(world: SenderWorld, timeout: float = 30.0) -> None:
    await wait_until(lambda: not world.panel.open_items(), timeout=timeout, what="the queue to be handled")


def first_results(world: SenderWorld) -> dict[str, dict[str, Any]]:
    """Первый итог по каждой строке: что бот сказал панели в самый первый раз."""
    found: dict[str, dict[str, Any]] = {}
    for body in world.panel.acks:
        for result in body["results"]:
            found.setdefault(result["id"], result)
    return found


# --- что и как отправляется ----------------------------------------------------------------------------------------


async def test_news_goes_to_each_chat_as_html_with_the_text_of_the_broadcast(sender: SenderWorld) -> None:
    ids = chats(3)
    queue_news_for(sender, ids)

    sender.start()
    await all_done(sender)

    assert [sender.texts_of(chat) for chat in ids] == [[NEWS]] * 3
    assert {message.parse_mode for message in sender.telegram.messages.values()} == {"HTML"}
    assert len(sender.panel.in_status("sent")) == 3


async def test_the_text_is_sent_exactly_as_the_panel_gave_it(sender: SenderWorld) -> None:
    text = '<a href="https://example.org/?a=1&amp;b=2">ссылка</a> &lt;3 &#128512;\n<pre>x</pre>'
    queue_news_for(sender, [CHAT], text)

    sender.start()
    await all_done(sender)

    assert sender.texts_of(CHAT) == [text]


async def test_a_config_link_goes_as_plain_text_made_from_the_template_without_parse_mode(
    sender: SenderWorld,
) -> None:
    item = sender.panel.queue_config(CHAT)

    sender.start()
    await all_done(sender)

    (url,) = item.urls
    assert sender.texts_of(CHAT) == [config_message(url)]
    assert {message.parse_mode for message in sender.telegram.messages.values()} == {None}


async def test_news_and_links_in_one_batch_each_keep_their_own_mode(sender: SenderWorld) -> None:
    queue_news_for(sender, [CHAT])
    sender.panel.queue_config(CHAT + 1)

    sender.start()
    await all_done(sender)

    modes = {chat: message.parse_mode for (chat, _), message in sender.telegram.messages.items()}
    assert modes == {CHAT: "HTML", CHAT + 1: None}


async def test_a_new_link_in_the_message_is_the_one_the_panel_issued_for_that_claim(sender: SenderWorld) -> None:
    sender.panel.queue_config(CHAT)
    sender.panel.queue_config(CHAT + 1)

    sender.start()
    await all_done(sender)

    live = {link.telegram_id: link.url for link in sender.panel.live_links()}
    for chat in (CHAT, CHAT + 1):
        assert sender.texts_of(chat) == [config_message(live[chat])]


async def test_the_bot_tells_the_panel_the_result_of_every_message(sender: SenderWorld) -> None:
    queue_news_for(sender, chats(4))

    sender.start()
    await all_done(sender)

    results = first_results(sender)
    assert set(results) == {item.id for item in sender.panel.outbox}
    assert {result["status"] for result in results.values()} == {"sent"}


async def test_an_empty_queue_is_asked_about_every_five_seconds_and_nothing_is_sent(sender: SenderWorld) -> None:
    sender.start()
    await wait_until(lambda: sender.clock.now >= 20, what="twenty virtual seconds")

    claims = [call.at for call in sender.panel.calls if call.route == OUTBOX_CLAIM]
    assert 4 <= len(claims) <= 6 and all(4.9 <= later - earlier <= 5.1 for earlier, later in zip(claims, claims[1:]))
    assert sender.sends() == 0 and sender.alive()


async def test_the_claim_asks_for_the_batch_size_from_the_settings(sender: SenderWorld) -> None:
    sender.start(batch=10)
    await wait_until(lambda: sender.panel.count(OUTBOX_CLAIM) >= 1, what="a claim")

    assert sender.panel.bodies(OUTBOX_CLAIM)[0] == {"limit": 10}


async def test_a_full_batch_is_followed_by_the_next_claim_without_the_five_second_wait(sender: SenderWorld) -> None:
    queue_news_for(sender, chats(120))

    sender.start(batch=50)
    await all_done(sender)

    times = [moment for moment, _, _ in sender.panel.claim_log]
    assert [len(ids) for _, _, ids in sender.panel.claim_log[:3]] == [50, 50, 20]
    assert times[1] - times[0] < 5 and times[2] - times[1] < 5


# --- темп ----------------------------------------------------------------------------------------------------------


async def test_a_thousand_messages_go_out_without_a_429_within_the_rate_in_about_fifty_seconds(
    sender: SenderWorld,
) -> None:
    queue_news_for(sender, chats(1000))

    sender.start()
    await all_done(sender, timeout=180)

    times = sender.telegram.delivery_times()
    assert sender.sends() == 1000 and len(times) == 1000
    assert sender.telegram.flood_errors == []  # ни одного 429
    assert sender.telegram.busiest_second() <= 20  # OUTBOX_RATE_PER_SECOND по умолчанию
    assert 48.9 <= times[-1] - times[0] <= 55  # около 50 с: быстрее 20 в секунду не получится, медленнее незачем
    assert len(sender.panel.in_status("sent")) == 1000 and len(first_results(sender)) == 1000


async def test_the_rate_follows_the_setting(sender: SenderWorld) -> None:
    queue_news_for(sender, chats(30))

    sender.start(rate_per_second=5)
    await all_done(sender)

    times = sender.telegram.delivery_times()
    assert sender.telegram.flood_errors == [] and sender.telegram.busiest_second() <= 5
    assert times[-1] - times[0] >= 4.9


async def test_messages_to_one_chat_are_at_least_a_second_apart_and_the_others_still_go(
    sender: SenderWorld,
) -> None:
    queue_news_for(sender, [CHAT] * 5 + chats(5, CHAT + 1))

    sender.start()
    await all_done(sender)

    own = sender.telegram.delivery_times(CHAT)
    assert len(own) == 5 and all(later - earlier >= 1 - EDGE for earlier, later in zip(own, own[1:]))
    assert sender.telegram.flood_errors == [] and len(sender.panel.in_status("sent")) == 10


async def test_a_news_and_a_link_for_one_person_are_a_second_apart(sender: SenderWorld) -> None:
    queue_news_for(sender, [CHAT])
    sender.panel.queue_config(CHAT)

    sender.start()
    await all_done(sender)

    own = sender.telegram.delivery_times(CHAT)
    assert len(own) == 2 and own[1] - own[0] >= 1 - EDGE and sender.telegram.flood_errors == []


# --- ответы Telegram -----------------------------------------------------------------------------------------------


async def test_a_429_pauses_the_whole_sender_for_retry_after_and_repeats_the_same_message(
    sender: SenderWorld,
) -> None:
    ids = chats(3)
    queue_news_for(sender, ids)
    sender.telegram.fail(SendMessage, retry_after_error(7))

    sender.start()
    await all_done(sender)

    assert sender.sends() == 4  # одна неудача и три отправки
    assert any(pause >= 7 for pause in sender.clock.sleeps)
    assert min(sender.telegram.delivery_times()) >= 7 - EDGE  # пока идёт пауза, не уходит ничего
    assert [sender.texts_of(chat) for chat in ids] == [[NEWS]] * 3  # каждому по разу, ни одно не потеряно
    assert {result["status"] for result in first_results(sender).values()} == {"sent"}


async def test_a_429_longer_than_what_is_left_of_the_lease_returns_the_messages_as_retry_flood(
    tmp_path: Path,
) -> None:
    async with running_sender(tmp_path) as world:
        world.panel.lease_seconds = 6
        queue_news_for(world, chats(3))
        world.telegram.fail(SendMessage, retry_after_error(30))

        world.start()
        await wait_until(lambda: len(first_results(world)) == 3, what="the results")

        results = first_results(world)
        assert world.telegram.deliveries == []
        assert {result["status"] for result in results.values()} == {"retry"}
        flooded = results[world.panel.outbox[0].id]
        assert flooded["error"] == "flood" and flooded["retry_after"] >= 30


async def test_403_marks_the_person_as_blocked_and_does_not_stop_the_others(sender: SenderWorld) -> None:
    ids = chats(3)
    queue_news_for(sender, ids)
    sender.telegram.forbidden.add(ids[1])

    sender.start()
    await all_done(sender)

    results = first_results(sender)
    by_chat = {item.telegram_id: results[item.id] for item in sender.panel.outbox}
    assert by_chat[ids[1]]["status"] == "blocked" and by_chat[ids[1]].get("error") in (None, "blocked")
    assert by_chat[ids[0]]["status"] == by_chat[ids[2]]["status"] == "sent"
    assert sender.texts_of(ids[1]) == [] and sender.sends() == 3  # заблокировавшему повторно не пишут


async def test_400_chat_not_found_fails_with_its_code(sender: SenderWorld) -> None:
    queue_news_for(sender, [CHAT])
    sender.telegram.rejected[CHAT] = "Bad Request: chat not found"

    sender.start()
    await all_done(sender)

    (result,) = first_results(sender).values()
    assert (result["status"], result["error"]) == ("failed", "chat_not_found")
    assert sender.panel.outbox[0].status == "failed"


async def test_any_other_400_fails_as_bad_request(sender: SenderWorld) -> None:
    queue_news_for(sender, [CHAT])
    sender.telegram.rejected[CHAT] = "Bad Request: can't parse entities: unsupported start tag"

    sender.start()
    await all_done(sender)

    (result,) = first_results(sender).values()
    assert (result["status"], result["error"]) == ("failed", "bad_request")


async def test_the_text_of_a_telegram_error_never_reaches_the_panel(sender: SenderWorld) -> None:
    queue_news_for(sender, [CHAT, CHAT + 1])
    sender.telegram.rejected[CHAT] = "Bad Request: SECRET-REASON-7731"
    sender.telegram.forbidden.add(CHAT + 1)

    sender.start()
    await all_done(sender)

    assert "SECRET-REASON-7731" not in str(sender.panel.acks) and "Forbidden" not in str(sender.panel.acks)
    errors = {result.get("error") for result in first_results(sender).values()}
    assert errors <= {None, "blocked", "bad_request", "chat_not_found"}


@pytest.mark.parametrize(("kind", "code"), [("network", "network"), ("server", "telegram_5xx")])
async def test_a_network_or_server_error_is_a_retry_and_the_message_goes_out_once_later(
    sender: SenderWorld, kind: str, code: str
) -> None:
    queue_news_for(sender, [CHAT])
    sender.telegram.fail(SendMessage, telegram_error(kind))

    sender.start()
    await all_done(sender)

    (first,) = first_results(sender).values()
    assert (first["status"], first["error"]) == ("retry", code)
    assert sender.texts_of(CHAT) == [NEWS] and sender.panel.outbox[0].status == "sent"
    assert sender.panel.outbox[0].attempts == 1  # повтор посчитан


# --- сбои панели ---------------------------------------------------------------------------------------------------


async def test_the_sender_lives_through_a_panel_outage_on_claim(sender: SenderWorld) -> None:
    queue_news_for(sender, chats(2))
    sender.panel.override(OUTBOX_CLAIM, unavailable(), times=6)

    sender.start()
    await all_done(sender)

    assert sender.alive() and len(sender.panel.in_status("sent")) == 2


async def test_the_sender_lives_through_a_rejected_key_and_goes_on_when_the_panel_takes_it_again(
    sender: SenderWorld,
) -> None:
    queue_news_for(sender, chats(2))
    sender.panel.override(OUTBOX_CLAIM, unauthorized(), times=2)

    sender.start()
    await all_done(sender)

    assert sender.alive() and len(sender.panel.in_status("sent")) == 2


async def test_the_sender_lives_through_a_long_rate_limit_of_the_panel(sender: SenderWorld) -> None:
    queue_news_for(sender, chats(2))
    sender.panel.override(OUTBOX_CLAIM, rate_limited(60), times=2)

    sender.start()
    await all_done(sender)

    assert sender.alive() and len(sender.panel.in_status("sent")) == 2


# --- аренда --------------------------------------------------------------------------------------------------------


async def test_a_lease_that_has_under_five_seconds_left_sends_nothing_and_returns_everything(
    tmp_path: Path,
) -> None:
    async with running_sender(tmp_path) as world:
        world.panel.lease_seconds = 4
        queue_news_for(world, chats(3))

        world.start()
        await wait_until(lambda: len(first_results(world)) == 3, what="the results")

        assert world.sends() == 0
        assert {result["status"] for result in first_results(world).values()} == {"retry"}


async def test_the_sender_stops_five_seconds_before_the_lease_ends_and_returns_the_rest(tmp_path: Path) -> None:
    async with running_sender(tmp_path) as world:
        world.panel.lease_seconds = 8
        queue_news_for(world, chats(100))

        world.start(batch=100)
        await wait_until(lambda: len(first_results(world)) == 100, what="the results of the first lease")

        started, lease, ids = world.panel.claim_log[0]
        results = [first_results(world)[item_id]["status"] for item_id in ids]
        before_the_next_claim = [moment for moment in world.telegram.delivery_times() if moment < started + 5]
        assert 0 < results.count("sent") < 100 and results.count("retry") == 100 - results.count("sent")
        assert max(before_the_next_claim) <= started + 8 - 5 + 1e-3  # ни одной отправки за 5 секунд до конца аренды
        assert world.telegram.flood_errors == [] and lease


# --- подтверждения -------------------------------------------------------------------------------------------------


async def test_results_wait_for_the_timer_and_go_to_the_panel_in_one_batch_after_about_two_seconds(
    sender: SenderWorld,
) -> None:
    queue_news_for(sender, chats(3))

    sender.start()
    await all_done(sender)

    acks = [call for call in sender.panel.calls if call.route == OUTBOX_ACK]
    assert len(acks) == 1 and len(acks[0].body["results"]) == 3
    assert 1.5 <= acks[0].at <= 3


async def test_twenty_five_results_are_sent_at_once_without_waiting_for_the_timer(tmp_path: Path) -> None:
    async with running_sender(tmp_path, drive=False) as world:  # время стоит: таймер сработать не может
        queue_news_for(world, chats(25))

        world.start(rate_per_second=28)
        await wait_until(lambda: world.sent_to_panel() == 25, what="the batch ack")

        assert len(world.panel.acks) == 1 and len(world.panel.acks[0]["results"]) == 25


async def test_twenty_four_results_still_wait(tmp_path: Path) -> None:
    async with running_sender(tmp_path, drive=False) as world:
        queue_news_for(world, chats(24))

        world.start(rate_per_second=28)
        await wait_until(lambda: len(world.journal()) == 24, what="24 messages in the journal")

        assert world.panel.acks == []


async def test_stopping_the_bot_sends_the_results_that_were_waiting(tmp_path: Path) -> None:
    async with running_sender(tmp_path, drive=False) as world:
        queue_news_for(world, chats(3))
        world.start()
        await wait_until(lambda: len(world.journal()) == 3, what="3 messages in the journal")
        assert world.panel.acks == []

        await world.stop()  # SIGTERM

        assert world.sent_to_panel() == 3 and world.journal() == []


async def test_the_journal_is_empty_once_the_panel_has_taken_the_results(sender: SenderWorld) -> None:
    queue_news_for(sender, chats(3))

    sender.start()
    await all_done(sender)
    await wait_until(lambda: sender.journal() == [], what="the journal to be cleaned")


async def test_while_the_panel_does_not_take_the_results_the_journal_keeps_them_and_nothing_is_sent_twice(
    sender: SenderWorld,
) -> None:
    ids = chats(3)
    queue_news_for(sender, ids)
    sender.panel.override(OUTBOX_ACK, unavailable(), times=1000)

    sender.start()
    await wait_until(lambda: len(sender.journal()) == 3, what="the journal")
    await wait_until(lambda: sender.clock.now >= 30, what="thirty virtual seconds")

    rows = sender.journal()
    lease = sender.panel.claim_log[0][1]
    assert sorted(row[0] for row in rows) == sorted(item.id for item in sender.panel.outbox)
    assert {(row[1], row[2], row[3]) for row in rows} == {(lease, "news", "sent")}
    assert sender.sends() == 3 and sender.alive()


async def test_the_journal_holds_neither_the_text_nor_the_address(sender: SenderWorld) -> None:
    queue_news_for(sender, [CHAT], NEWS)
    item = sender.panel.queue_config(CHAT + 1)
    sender.panel.override(OUTBOX_ACK, unavailable(), times=1000)

    sender.start()
    await wait_until(lambda: len(sender.journal()) == 2, what="the journal")

    dump = str(sender.journal())
    assert "Плановые" not in dump and "/c#" not in dump and str(CHAT) not in dump
    assert item.urls[0] not in dump


async def test_a_news_message_that_comes_back_in_a_new_lease_is_acked_without_being_sent_again(
    tmp_path: Path,
) -> None:
    async with running_sender(tmp_path) as world:
        world.panel.lease_seconds = 20
        queue_news_for(world, chats(3))
        world.panel.override(OUTBOX_ACK, unavailable(), times=1000)
        world.panel.at(40, world.panel.recover)  # к этому времени аренда вышла и строки уже выданы заново

        world.start()
        await all_done(world, timeout=60)

        assert world.sends() == 3  # Telegram принял каждое один раз
        assert len({lease for _, lease, _ in world.panel.claim_log}) >= 2  # строки выдавались снова
        assert [world.texts_of(chat) for chat in chats(3)] == [[NEWS]] * 3
        await wait_until(lambda: world.journal() == [], what="the journal to be cleaned")


# --- перезапуск до подтверждения -----------------------------------------------------------------------------------


async def test_after_a_restart_the_unacked_news_is_confirmed_by_the_old_lease_and_not_sent_again(
    tmp_path: Path,
) -> None:
    async with running_sender(tmp_path) as world:
        world.panel.lease_seconds = 600
        queue_news_for(world, chats(3))
        world.panel.override(OUTBOX_ACK, unavailable(), times=1000)
        world.start()
        await wait_until(lambda: len(world.journal()) == 3, what="the journal")
        first_lease = world.panel.claim_log[0][1]
        await world.stop()  # процесс остановлен до того, как панель приняла итоги
        assert len(world.journal()) == 3 and world.sent_to_panel() == 0
        world.panel.recover()

        await world.restart()
        await wait_until(lambda: world.sent_to_panel() == 3, what="the old results to be confirmed")

        assert world.sends() == 3  # заново не отправлено ничего
        assert any(body["lease_id"] == first_lease for body in world.panel.acks)
        await wait_until(lambda: world.journal() == [], what="the journal to be cleaned")


async def test_after_a_restart_a_news_message_that_came_in_a_new_lease_is_acked_without_being_sent_again(
    tmp_path: Path,
) -> None:
    async with running_sender(tmp_path) as world:
        queue_news_for(world, chats(3))
        world.panel.override(OUTBOX_ACK, unavailable(), times=1000)
        world.start()
        await wait_until(lambda: len(world.journal()) == 3, what="the journal")
        first_lease = world.panel.claim_log[0][1]
        await world.stop()
        world.panel.steal_leases(30)  # строки ушли в другую аренду, старое подтверждение теперь не примут
        world.panel.recover()

        await world.restart()
        await wait_until(lambda: world.sent_to_panel() == 3, timeout=30, what="the new lease to be confirmed")

        assert world.sends() == 3
        assert {lease for _, lease, _ in world.panel.claim_log} - {first_lease}  # выдача в новой аренде была
        assert [world.texts_of(chat) for chat in chats(3)] == [[NEWS]] * 3


async def test_after_a_restart_a_link_that_comes_in_a_new_lease_is_sent_again_because_it_is_a_new_link(
    tmp_path: Path,
) -> None:
    async with running_sender(tmp_path) as world:
        item = world.panel.queue_config(CHAT)
        world.panel.override(OUTBOX_ACK, unavailable(), times=1000)
        world.start()
        await wait_until(lambda: len(world.journal()) == 1, what="the journal")
        await world.stop()
        world.panel.steal_leases(30)
        world.panel.recover()

        await world.restart()
        await wait_until(lambda: world.sent_to_panel() == 1, timeout=30, what="the new lease to be confirmed")

        first, second = item.urls
        assert first != second
        assert world.texts_of(CHAT) == [config_message(first), config_message(second)]
        assert [link.url for link in world.panel.live_links(CHAT)] == [second]  # прежняя гаснет, и это ожидаемо


async def test_after_a_restart_the_loop_goes_on_with_the_queue(tmp_path: Path) -> None:
    async with running_sender(tmp_path) as world:
        queue_news_for(world, chats(2))
        world.start()
        await all_done(world)
        await world.restart()
        queue_news_for(world, chats(2, CHAT + 10))

        await all_done(world)

        assert world.sends() == 4 and world.alive()


async def test_purge_removes_journal_records_older_than_a_day_and_keeps_the_fresh_ones(tmp_path: Path) -> None:
    async with running_sender(tmp_path) as world:
        await world.storage.purge()  # открыть файл и создать таблицы
        now = world.clock.wall()
        world.put_in_journal(
            [
                ("old", "lease-1", "news", "sent", None, now - 25 * 3600),
                ("fresh", "lease-2", "news", "sent", None, now - 23 * 3600),
            ]
        )

        await world.storage.purge()

        assert [row[0] for row in world.journal()] == ["fresh"]


# --- настройки -----------------------------------------------------------------------------------------------------


def test_the_defaults_are_twenty_a_second_in_batches_of_fifty(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert (settings.outbox_rate_per_second, settings.outbox_batch) == (20, 50)


@pytest.mark.parametrize("rate", [1, 28])
def test_a_rate_inside_the_limit_is_accepted(tmp_path: Path, rate: int) -> None:
    assert make_settings(tmp_path, outbox_rate_per_second=rate).outbox_rate_per_second == rate


@pytest.mark.parametrize("rate", [0, -1, 29, 30])
def test_a_rate_that_would_run_into_the_telegram_limit_is_refused(tmp_path: Path, rate: int) -> None:
    """Запас под ответы людям в диалогах: лимит Telegram - 30 в секунду на весь токен."""
    with pytest.raises(ValidationError):
        make_settings(tmp_path, outbox_rate_per_second=rate)


@pytest.mark.parametrize("batch", [1, 100])
def test_a_batch_inside_the_limit_is_accepted(tmp_path: Path, batch: int) -> None:
    assert make_settings(tmp_path, outbox_batch=batch).outbox_batch == batch


@pytest.mark.parametrize("batch", [0, -1, 101])
def test_a_batch_outside_the_limit_is_refused(tmp_path: Path, batch: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, outbox_batch=batch)


def test_the_settings_are_read_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTBOX_RATE_PER_SECOND", "12")
    monkeypatch.setenv("OUTBOX_BATCH", "30")

    settings = make_settings(tmp_path)

    assert (settings.outbox_rate_per_second, settings.outbox_batch) == (12, 30)


# --- логи ----------------------------------------------------------------------------------------------------------


async def test_neither_the_text_nor_the_address_of_a_link_gets_into_the_log_whatever_goes_wrong(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    setup_logging(make_settings(tmp_path, level="DEBUG"), stream)
    root.addHandler(caplog.handler)  # настройка логов бота убрала обработчик pytest: сырые записи нужны тоже
    try:
        async with running_sender(tmp_path) as world:
            marker = "Секретная-новость-5521"
            queue_news_for(world, chats(5), marker)
            item = world.panel.queue_config(CHAT + 50)
            world.telegram.forbidden.add(CHAT)
            world.telegram.rejected[CHAT + 1] = f"Bad Request: chat not found {marker}"
            world.telegram.fail(SendMessage, telegram_error("network", marker))
            world.telegram.fail(SendMessage, retry_after_error(3))
            world.panel.override(OUTBOX_CLAIM, unavailable(), times=5)

            world.start()
            await all_done(world)
            await world.stop()
            output = stream.getvalue() + caplog.text + str([vars(record) for record in caplog.records])

            assert marker not in output and "Плановые" not in output
            assert item.urls[0] not in output and "/c#" not in output
            assert "OutboxLnk" not in output
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)
