"""Отправитель очереди, вид `notify` (этап 25.5, Р3, Р7): текст пишет панель, бот пересылает как есть.

- бот просит у панели `news`, `config` и `notify`;
- `plain` уходит без разметки, `html` - с `parse_mode=HTML`; превью ссылок выключено;
- итоги те же, что у `news` (sent / blocked / failed / retry), журнал - тоже: принятое Telegram не уходит второй раз;
- текст уведомления не попадает ни в лог, ни в журнал, ни в `repr`."""

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiogram.methods import SendMessage

from fakes import OUTBOX_ACK, unavailable
from support import SenderWorld, make_settings, running_sender, setup_logging, telegram_error, wait_until

ADMIN_CHAT = 6660001234
TEXT = "Сервер alpha недоступен\nВремя: 12:30 UTC"
HTML = "<b>Проба</b> уведомления"
SECRET = "Секретная-строка-уведомления-7731"


@pytest.fixture
async def sender(tmp_path: Path) -> AsyncIterator[SenderWorld]:
    async with running_sender(tmp_path) as running:
        yield running


async def all_done(world: SenderWorld) -> None:
    await wait_until(lambda: not world.panel.open_items(), timeout=30, what="the queue to be handled")


async def test_the_bot_asks_the_panel_for_notify_rows(sender: SenderWorld) -> None:
    sender.start()
    await wait_until(lambda: bool(sender.panel.claim_kinds), what="the first claim")

    assert {"news", "config", "notify"} <= set(sender.panel.claim_kinds[0])


async def test_plain_text_goes_out_as_it_is_without_markup(sender: SenderWorld) -> None:
    sender.panel.queue_notify(ADMIN_CHAT, TEXT)

    sender.start()
    await all_done(sender)

    assert sender.texts_of(ADMIN_CHAT) == [TEXT]
    assert sender.telegram.parse_modes() == {None}
    assert len(sender.panel.in_status("sent")) == 1


async def test_a_plain_text_that_looks_like_markup_is_not_parsed(sender: SenderWorld) -> None:
    tricky = "сервер <b>alpha</b> & beta"
    sender.panel.queue_notify(ADMIN_CHAT, tricky)

    sender.start()
    await all_done(sender)

    assert sender.texts_of(ADMIN_CHAT) == [tricky] and sender.telegram.parse_modes() == {None}


async def test_html_text_goes_out_with_the_html_parse_mode(sender: SenderWorld) -> None:
    sender.panel.queue_notify(ADMIN_CHAT, HTML, fmt="html")

    sender.start()
    await all_done(sender)

    assert sender.texts_of(ADMIN_CHAT) == [HTML] and sender.telegram.parse_modes() == {"HTML"}


async def test_news_and_notify_in_one_pack_keep_their_own_modes(sender: SenderWorld) -> None:
    sender.panel.queue_news(ADMIN_CHAT + 1, "<i>новость</i>")
    sender.panel.queue_notify(ADMIN_CHAT, TEXT)

    sender.start()
    await all_done(sender)

    modes = {sent.chat_id: sent.parse_mode for sent in sender.telegram.messages.values()}
    assert modes == {ADMIN_CHAT + 1: "HTML", ADMIN_CHAT: None}


async def test_a_blocked_admin_chat_is_reported_as_blocked(sender: SenderWorld) -> None:
    item = sender.panel.queue_notify(ADMIN_CHAT, TEXT)
    sender.telegram.forbidden.add(ADMIN_CHAT)

    sender.start()
    await all_done(sender)

    assert item.status == "blocked" and sender.texts_of(ADMIN_CHAT) == []


async def test_a_rejected_chat_is_a_failure_with_a_code_not_a_telegram_text(sender: SenderWorld) -> None:
    item = sender.panel.queue_notify(ADMIN_CHAT, TEXT)
    sender.telegram.rejected[ADMIN_CHAT] = f"Bad Request: chat not found {SECRET}"

    sender.start()
    await all_done(sender)

    assert item.status == "failed" and item.error in ("chat_not_found", "bad_request")
    assert SECRET not in str(sender.panel.acks)


async def test_a_network_error_returns_the_row_for_a_retry_and_it_is_delivered_later(sender: SenderWorld) -> None:
    item = sender.panel.queue_notify(ADMIN_CHAT, TEXT)
    sender.telegram.fail(SendMessage, telegram_error("network"), times=1)

    sender.start()
    await all_done(sender)

    assert item.status == "sent" and sender.texts_of(ADMIN_CHAT) == [TEXT]


async def test_after_a_restart_a_notify_that_came_in_a_new_lease_is_acked_without_being_sent_again(
    tmp_path: Path,
) -> None:
    async with running_sender(tmp_path) as world:
        world.panel.queue_notify(ADMIN_CHAT, TEXT)
        world.panel.override(OUTBOX_ACK, unavailable(), times=1000)
        world.start()
        await wait_until(lambda: len(world.journal()) == 1, what="the journal")
        await world.stop()
        world.panel.steal_leases(30)
        world.panel.recover()

        await world.restart()
        await wait_until(lambda: world.sent_to_panel() == 1, timeout=30, what="the new lease to be confirmed")

        assert world.sends() == 1 and world.texts_of(ADMIN_CHAT) == [TEXT]


async def test_the_journal_holds_the_kind_and_never_the_text(sender: SenderWorld) -> None:
    sender.panel.queue_notify(ADMIN_CHAT, f"{TEXT} {SECRET}")
    sender.panel.override(OUTBOX_ACK, unavailable(), times=1000)

    sender.start()
    await wait_until(lambda: len(sender.journal()) == 1, what="the journal")

    ((_, _, kind, status, _),) = sender.journal()
    assert (kind, status) == ("notify", "sent")
    assert SECRET not in sender.state_file.read_bytes().decode("utf-8", "ignore")


async def test_the_text_and_the_chat_do_not_reach_the_log(tmp_path: Path) -> None:
    stream = io.StringIO()
    setup_logging(make_settings(tmp_path, level="DEBUG"), stream)
    async with running_sender(tmp_path) as world:
        world.panel.queue_notify(ADMIN_CHAT, f"{TEXT} {SECRET}")
        world.start()
        await all_done(world)

    assert SECRET not in stream.getvalue() and str(ADMIN_CHAT) not in stream.getvalue()


async def test_the_item_repr_does_not_show_the_text() -> None:
    from tollwire_bot.panel import OutboxItem

    item = OutboxItem(id="1", kind="notify", telegram_id=ADMIN_CHAT, text=SECRET, format="plain")  # type: ignore[call-arg]

    assert SECRET not in repr(item) and str(ADMIN_CHAT) not in repr(item)
    assert item.text == SECRET and item.format == "plain"  # type: ignore[attr-defined]
