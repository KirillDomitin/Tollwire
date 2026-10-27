"""Watcher: после «Готовим доступ…» бот опрашивает GET /registrations/{id} и сообщает итог человеку.

Расписание: через 2, 5, 10 с, затем каждые 10 с, всего до 3 минут. Итог: active (бот просит у панели ссылку на страницу
конфига: `POST .../config-link`), failed, таймаут; сбои панели и «клиент ещё выдаётся» - повтор на следующем опросе.
Список ожидающих лежит в хранилище: после рестарта бота опрос продолжается.
Время виртуальное (FakeClock): три минуты проходят за доли секунды, моменты запросов точны."""

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from fakes import CONFIG_LINK, conflict, rate_limited, unavailable
from support import (
    CODE,
    OTHER_CODE,
    OTHER_EMAIL,
    OTHER_TG_ID,
    K,
    Person,
    World,
    ready_link_text,
    settle,
    t,
    wait_until,
    watch_ids,
)

POLL = "GET /registrations/*"


@pytest.fixture
def ivan(world: World) -> Person:
    world.panel.add_invite(CODE)
    return world.person()


def polls(world: World) -> list[float]:
    """Моменты запросов статуса регистрации, секунды от начала теста."""
    return [call.at for call in world.panel.calls if call.route == POLL]


def started_at(world: World) -> float:
    return next(call.at for call in world.panel.calls if call.route == "POST /registrations")


def said_times(world: World, person: Person, text: str) -> list[float]:
    return world.telegram.when(person.id, text)


async def heard(person: Person, text: str) -> None:
    await wait_until(lambda: text in person.texts, what="the bot to say it")


async def heard_link(person: Person) -> str:
    """Ждёт сообщение «доступ готов» со ссылкой и возвращает ссылку; текст сообщения - ровно `ready_link`."""
    await wait_until(lambda: bool(person.links()), what="the bot to send the link")
    (url,) = person.links()
    assert ready_link_text(url) in person.texts
    return url


# --- расписание ------------------------------------------------------------------------------------------------------


async def test_polling_starts_after_2_3_and_5_seconds_and_then_goes_every_10(ivan: Person, world: World) -> None:
    await ivan.register()
    await heard(ivan, t(K.still_preparing))  # доходим до таймаута, чтобы увидеть всё расписание

    start = started_at(world)
    offsets = [round(at - start, 3) for at in polls(world)]
    assert offsets[:3] == [2, 5, 10]
    assert {round(later - earlier, 3) for earlier, later in zip(offsets[2:], offsets[3:])} == {10}


async def test_polling_gives_up_after_three_minutes_and_says_to_check_status(ivan: Person, world: World) -> None:
    await ivan.register()
    await heard(ivan, t(K.still_preparing))
    count = world.panel.count(POLL)

    (said,) = said_times(world, ivan, t(K.still_preparing))
    start = started_at(world)
    assert 175 <= said - start <= 195
    assert 15 <= count <= 30
    assert polls(world)[-1] - start >= 170  # не сдались раньше времени

    await settle()
    assert world.panel.count(POLL) == count  # после итога запросов больше нет
    assert await watch_ids(world.storage) == []  # и в списке ожидающих не осталось


# --- итоги -----------------------------------------------------------------------------------------------------------


async def test_ready_asks_the_panel_for_a_link_and_gives_it_with_the_date(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.at(7, lambda: world.panel.finish(world.panel.only_registration()))

    url = await heard_link(ivan)

    (said,) = said_times(world, ivan, ready_link_text(url))
    assert said - started_at(world) == 10  # статус стал active в 7 с, третий опрос - на 10-й
    assert world.panel.routes()[-1] == CONFIG_LINK  # ссылку и срок бот берёт у панели, а не из статуса регистрации
    assert world.panel.live_links(ivan.id)[0].url == url


async def test_the_link_is_requested_for_the_person_by_the_path_with_an_empty_body(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.finish(world.panel.only_registration())

    await heard_link(ivan)

    (call,) = [call for call in world.panel.calls if call.route == CONFIG_LINK]
    assert call.path.endswith(f"/users/by-telegram/{ivan.id}/config-link")
    assert call.method == "POST" and not call.body


async def test_while_the_client_is_not_ready_the_next_poll_asks_again(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.override(CONFIG_LINK, conflict("not_ready"), times=2)
    world.panel.finish(world.panel.only_registration())

    url = await heard_link(ivan)

    (said,) = said_times(world, ivan, ready_link_text(url))
    assert said - started_at(world) == 10  # опросы на 2 и 5 с получили not_ready, на 10 с - ссылку
    assert world.panel.count(CONFIG_LINK) == 3 and len(world.panel.links) == 1
    assert t(K.still_preparing) not in ivan.texts and t(K.config_not_ready) not in ivan.texts


async def test_a_panel_failure_on_the_link_request_does_not_end_the_polling(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.override(CONFIG_LINK, unavailable(), times=5)  # клиент панели исчерпывает повторы и сдаётся
    world.panel.finish(world.panel.only_registration())

    await heard_link(ivan)

    assert t(K.failed) not in ivan.texts and t(K.still_preparing) not in ivan.texts
    assert world.panel.count(CONFIG_LINK) >= 2


async def test_a_rate_limit_on_the_link_request_does_not_end_the_polling(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.override(CONFIG_LINK, rate_limited(60), times=2)
    world.panel.finish(world.panel.only_registration())

    await heard_link(ivan)


@pytest.mark.parametrize(
    ("reason", "key"), [("config_unavailable", K.config_unavailable), ("no_access", K.config_no_access)]
)
async def test_a_client_that_will_never_have_a_page_gets_its_own_text_and_the_polling_ends(
    ivan: Person, world: World, reason: str, key: str
) -> None:
    await ivan.register()
    world.panel.link_refusal[ivan.id] = reason
    world.panel.finish(world.panel.only_registration())

    await heard(ivan, t(key))
    await settle()

    assert ivan.texts.count(t(key)) == 1 and not ivan.links()
    assert world.panel.count(CONFIG_LINK) == 1  # дальше не спрашиваем: ответ не изменится
    assert await watch_ids(world.storage) == []
    assert t(K.failed) not in ivan.texts and t(K.still_preparing) not in ivan.texts


async def test_a_failed_send_is_retried_with_a_new_link_and_the_old_one_is_dead(ivan: Person, world: World) -> None:
    await ivan.register()
    world.telegram.fail(SendMessage, RuntimeError("network is down"))
    world.panel.finish(world.panel.only_registration())

    url = await heard_link(ivan)

    assert world.panel.count(CONFIG_LINK) == 2 and len(world.panel.links) == 2
    first, second = world.panel.links
    assert not first.live and second.live and second.url == url  # человек получил ту ссылку, что действует
    assert first.url not in "\n".join(ivan.texts)
    assert await watch_ids(world.storage) == []


async def test_a_finished_registration_with_a_link_is_not_announced_twice(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.finish(world.panel.only_registration())

    await heard_link(ivan)
    await settle()

    assert len(ivan.links()) == 1 and len(world.panel.links) == 1


async def test_failed_says_we_will_sort_it_out(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.at(3, lambda: world.panel.finish(world.panel.only_registration(), "failed"))

    await heard(ivan, t(K.failed))

    assert ivan.texts.count(t(K.failed)) == 1
    assert not ivan.links() and t(K.still_preparing) not in ivan.texts
    assert world.panel.count(CONFIG_LINK) == 0  # по упавшей регистрации ссылку не просят


@pytest.mark.parametrize("status", ["active", "failed"])
async def test_a_finished_registration_stops_the_polling_and_leaves_the_list(
    ivan: Person, world: World, status: str
) -> None:
    await ivan.register()
    world.panel.at(1, lambda: world.panel.finish(world.panel.only_registration(), status))

    if status == "active":
        await heard_link(ivan)
    else:
        await heard(ivan, t(K.failed))
    await settle()

    assert world.panel.count(POLL) == 1  # статус уже был итоговым на первом опросе
    assert await watch_ids(world.storage) == []


async def test_the_polling_that_never_sees_active_gives_no_link_and_asks_for_none(ivan: Person, world: World) -> None:
    await ivan.register()
    await heard(ivan, t(K.still_preparing))

    assert not ivan.links() and world.panel.count(CONFIG_LINK) == 0


# --- сбои на пути ----------------------------------------------------------------------------------------------------


async def test_panel_outage_between_polls_does_not_kill_the_polling(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.override(POLL, unavailable(), times=12)
    world.panel.at(20, lambda: world.panel.finish(world.panel.only_registration()))

    await heard_link(ivan)

    assert t(K.still_preparing) not in ivan.texts


async def test_a_rate_limit_between_polls_does_not_kill_the_polling(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.override(POLL, rate_limited(60), times=3)
    world.panel.at(5, lambda: world.panel.finish(world.panel.only_registration()))

    await heard_link(ivan)


async def test_a_person_who_blocked_the_bot_does_not_break_anything(ivan: Person, world: World) -> None:
    await ivan.register()
    forbidden = TelegramForbiddenError(
        method=SendMessage(chat_id=ivan.id, text="x"), message="Forbidden: bot was blocked by the user"
    )
    world.telegram.fail(SendMessage, forbidden)
    world.panel.finish(world.panel.only_registration())

    await wait_until(lambda: world.telegram.count(SendMessage) >= 2, what="the attempt to tell the person")
    await wait_until(lambda: world.panel.count(CONFIG_LINK) >= 1, what="the final step")
    await settle()

    assert await watch_ids(world.storage) == []  # ждать больше нечего: сказать некому


async def test_a_registration_the_panel_refused_is_never_watched(ivan: Person, world: World) -> None:
    world.panel.override("POST /registrations", conflict("email_taken"))
    await ivan.to_confirm()

    await ivan.press(K.btn_yes)
    await settle()

    assert world.panel.count(POLL) == 0 and await watch_ids(world.storage) == []


# --- несколько человек -----------------------------------------------------------------------------------------------


async def test_every_person_hears_only_about_their_own_registration(ivan: Person, world: World) -> None:
    world.panel.add_invite(OTHER_CODE)
    maria = world.person(OTHER_TG_ID, "maria")
    await ivan.register()
    await maria.register(OTHER_CODE, "Мария", OTHER_EMAIL)
    first_id, second_id = list(world.panel.registrations)
    world.panel.finish(first_id)
    world.panel.at(30, lambda: world.panel.finish(second_id, "failed"))

    await heard_link(ivan)
    await heard(maria, t(K.failed))

    assert t(K.failed) not in ivan.texts
    assert not maria.links() and world.panel.live_links(maria.id) == []


# --- рестарт бота ----------------------------------------------------------------------------------------------------


async def test_polling_continues_after_a_restart(ivan: Person, world: World) -> None:
    await ivan.register()
    registration_id = world.panel.only_registration()
    await world.restart()
    seen = world.panel.count(POLL)

    world.panel.finish(registration_id)
    await heard_link(ivan)

    assert world.panel.count(POLL) > seen
    assert ivan.texts.count(t(K.preparing)) == 1  # «Готовим доступ…» заново не повторяли
    await settle()
    assert len(ivan.links()) == 1  # и итог не задвоился
    assert await watch_ids(world.storage) == []


async def test_a_finished_registration_is_not_announced_again_after_a_restart(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.finish(world.panel.only_registration())
    await heard_link(ivan)
    await settle()

    await world.restart()
    await settle()

    assert len(ivan.links()) == 1 and len(world.panel.links) == 1  # новой ссылки после рестарта никто не просил


async def test_the_three_minutes_are_counted_from_the_start_not_from_the_restart(ivan: Person, world: World) -> None:
    await ivan.register()
    start = started_at(world)
    await world.stop()  # бот лежал 170 секунд
    world.clock.advance(170)
    await world.boot()

    await heard(ivan, t(K.still_preparing))

    (said,) = said_times(world, ivan, t(K.still_preparing))
    assert 175 <= said - start <= 200  # а не 350: оставшиеся ~10 с, а не новые 180


async def test_a_registration_older_than_the_storage_lifetime_is_dropped_quietly(ivan: Person, world: World) -> None:
    await ivan.register()
    await world.stop()
    world.clock.advance(25 * 60 * 60)  # бот лежал сутки и час
    await world.boot()
    await settle()

    assert await watch_ids(world.storage) == []
    assert not ivan.links()


async def test_the_resumed_polling_asks_the_panel_for_the_same_registration(ivan: Person, world: World) -> None:
    await ivan.register()
    registration_id = world.panel.only_registration()
    await world.restart()

    await wait_until(lambda: world.panel.count(POLL) >= 1, what="a poll after the restart")

    last_poll = next(call for call in reversed(world.panel.calls) if call.route == POLL)
    assert last_poll.path.endswith(f"/registrations/{registration_id}")
