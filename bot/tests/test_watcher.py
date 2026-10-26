"""Watcher: после «Готовим доступ…» бот опрашивает GET /registrations/{id} и сообщает итог человеку.

Расписание: через 2, 3, 5 с, затем каждые 10 с, всего до 3 минут. Итог: active (ссылка или заглушка «конфиг пришлёт
администратор»), failed, таймаут. Список ожидающих лежит в хранилище: после рестарта бота опрос продолжается.
Время виртуальное (FakeClock): три минуты проходят за доли секунды, моменты запросов точны."""

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from fakes import conflict, rate_limited, unavailable
from support import (
    CODE,
    FAKE_UNTIL,
    OTHER_CODE,
    OTHER_EMAIL,
    OTHER_TG_ID,
    K,
    Person,
    World,
    settle,
    t,
    wait_until,
    watch_ids,
)

POLL = "GET /registrations/*"
BY_TELEGRAM = "GET /users/by-telegram/*"
LINK = "https://panel.example.test/c/ConfigPageToken"


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


async def test_ready_without_a_config_page_gives_the_date_and_says_the_admin_will_send_it(
    ivan: Person, world: World
) -> None:
    await ivan.register()
    world.panel.at(7, lambda: world.panel.finish(world.panel.only_registration()))

    await heard(ivan, t(K.ready_stub, until=FAKE_UNTIL))

    (said,) = said_times(world, ivan, t(K.ready_stub, until=FAKE_UNTIL))
    assert said - started_at(world) == 10  # статус стал active в 7 с, третий опрос - на 10-й
    assert world.panel.routes()[-1] == BY_TELEGRAM  # дата доступа - из /users/by-telegram


async def test_ready_with_a_config_page_gives_the_link_and_no_stub(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.at(3, lambda: world.panel.finish(world.panel.only_registration(), config_url=LINK))

    await heard(ivan, t(K.ready_link, url=LINK))

    assert t(K.ready_stub, until=FAKE_UNTIL) not in ivan.texts


async def test_failed_says_we_will_sort_it_out(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.at(3, lambda: world.panel.finish(world.panel.only_registration(), "failed"))

    await heard(ivan, t(K.failed))

    assert ivan.texts.count(t(K.failed)) == 1
    assert not any(text in ivan.texts for text in (t(K.ready_stub, until=FAKE_UNTIL), t(K.still_preparing)))


@pytest.mark.parametrize("status", ["active", "failed"])
async def test_a_finished_registration_stops_the_polling_and_leaves_the_list(
    ivan: Person, world: World, status: str
) -> None:
    await ivan.register()
    world.panel.at(1, lambda: world.panel.finish(world.panel.only_registration(), status))

    await heard(ivan, t(K.ready_stub, until=FAKE_UNTIL) if status == "active" else t(K.failed))
    await settle()

    assert world.panel.count(POLL) == 1  # статус уже был итоговым на первом опросе
    assert await watch_ids(world.storage) == []


async def test_the_final_message_comes_once(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.finish(world.panel.only_registration())

    await heard(ivan, t(K.ready_stub, until=FAKE_UNTIL))
    await settle()

    assert ivan.texts.count(t(K.ready_stub, until=FAKE_UNTIL)) == 1


# --- сбои на пути ----------------------------------------------------------------------------------------------------


async def test_panel_outage_between_polls_does_not_kill_the_polling(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.override(POLL, unavailable(), times=12)
    world.panel.at(20, lambda: world.panel.finish(world.panel.only_registration()))

    await heard(ivan, t(K.ready_stub, until=FAKE_UNTIL))

    assert t(K.still_preparing) not in ivan.texts


async def test_a_rate_limit_between_polls_does_not_kill_the_polling(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.override(POLL, rate_limited(60), times=3)
    world.panel.at(5, lambda: world.panel.finish(world.panel.only_registration()))

    await heard(ivan, t(K.ready_stub, until=FAKE_UNTIL))


async def test_a_person_who_blocked_the_bot_does_not_break_anything(ivan: Person, world: World) -> None:
    await ivan.register()
    forbidden = TelegramForbiddenError(
        method=SendMessage(chat_id=ivan.id, text="x"), message="Forbidden: bot was blocked by the user"
    )
    world.telegram.fail(SendMessage, forbidden)
    world.panel.finish(world.panel.only_registration())

    await wait_until(lambda: world.telegram.count(SendMessage) >= 2, what="the attempt to tell the person")
    await wait_until(lambda: world.panel.count(BY_TELEGRAM) >= 1, what="the final step")
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

    await heard(ivan, t(K.ready_stub, until=FAKE_UNTIL))
    await heard(maria, t(K.failed))

    assert t(K.failed) not in ivan.texts
    assert t(K.ready_stub, until=FAKE_UNTIL) not in maria.texts


# --- рестарт бота ----------------------------------------------------------------------------------------------------


async def test_polling_continues_after_a_restart(ivan: Person, world: World) -> None:
    await ivan.register()
    registration_id = world.panel.only_registration()
    await world.restart()
    seen = world.panel.count(POLL)

    world.panel.finish(registration_id)
    await heard(ivan, t(K.ready_stub, until=FAKE_UNTIL))

    assert world.panel.count(POLL) > seen
    assert ivan.texts.count(t(K.preparing)) == 1  # «Готовим доступ…» заново не повторяли
    await settle()
    assert ivan.texts.count(t(K.ready_stub, until=FAKE_UNTIL)) == 1  # и итог не задвоился
    assert await watch_ids(world.storage) == []


async def test_a_finished_registration_is_not_announced_again_after_a_restart(ivan: Person, world: World) -> None:
    await ivan.register()
    world.panel.finish(world.panel.only_registration())
    await heard(ivan, t(K.ready_stub, until=FAKE_UNTIL))
    await settle()

    await world.restart()
    await settle()

    assert ivan.texts.count(t(K.ready_stub, until=FAKE_UNTIL)) == 1


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
    assert t(K.ready_stub, until=FAKE_UNTIL) not in ivan.texts


async def test_the_resumed_polling_asks_the_panel_for_the_same_registration(ivan: Person, world: World) -> None:
    await ivan.register()
    registration_id = world.panel.only_registration()
    await world.restart()

    await wait_until(lambda: world.panel.count(POLL) >= 1, what="a poll after the restart")

    last_poll = next(call for call in reversed(world.panel.calls) if call.route == POLL)
    assert last_poll.path.endswith(f"/registrations/{registration_id}")
