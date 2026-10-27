"""Диалог регистрации (ТЗ 6.1) и команды: /start <код>, имя, почта, подтверждение, каждый отказ панели, /status, /help.

Бот - настоящий (диспетчер, хранилище в файле, клиент панели); Telegram и панель - фейки. Проверяется то, что видит
человек: какие тексты и кнопки ему пришли, что ушло в панель, чем кончился диалог. Ожидание опроса готовности
(`watcher`) - в test_watcher.py; здесь регистрация доходит до «Готовим доступ…»."""

import asyncio
import sqlite3
from typing import Any

import pytest

from fakes import conflict, rate_limited, rejected, unauthorized, unavailable
from support import (
    CODE,
    EMAIL,
    FAKE_UNTIL,
    NAME,
    OTHER_CODE,
    OTHER_EMAIL,
    OTHER_TG_ID,
    TG_ID,
    K,
    Person,
    World,
    settle,
    t,
    wait_until,
    watch_ids,
)

CHECK, BY_TELEGRAM, REGISTER = "POST /invites/check", "GET /users/by-telegram/*", "POST /registrations"


@pytest.fixture
def ivan(world: World) -> Person:
    world.panel.add_invite(CODE)
    return world.person()


def posted(world: World) -> list[dict[str, Any]]:
    return world.panel.bodies(REGISTER)


# --- /start ----------------------------------------------------------------------------------------------------------


async def test_start_without_a_code_says_the_service_is_closed(world: World) -> None:
    ivan = world.person()

    await ivan.say("/start")

    assert ivan.last == t(K.no_code)
    assert world.panel.calls == []  # проверять нечего: до панели дело не дошло


@pytest.mark.parametrize("code", ["bad code!", "код", "x" * 65, "a;b", "a/b", "a.b", "<b>"])
async def test_a_malformed_code_is_refused_without_asking_the_panel(world: World, code: str) -> None:
    ivan = world.person()

    await ivan.start(code)

    assert ivan.last == t(K.bad_link)
    assert world.panel.calls == []


@pytest.mark.parametrize("code", ["a", "A1_-", "x" * 64])
async def test_codes_of_the_allowed_shape_go_to_the_panel(world: World, code: str) -> None:
    ivan = world.person()

    await ivan.start(code)

    assert world.panel.bodies(CHECK) == [{"code": code}]


async def test_an_already_registered_person_is_sent_to_status(world: World) -> None:
    world.panel.add_invite(CODE)
    world.panel.add_user(TG_ID)
    ivan = world.person()

    await ivan.start()

    assert ivan.last == t(K.already_registered)
    assert world.panel.routes() == [BY_TELEGRAM]  # приглашение даже не проверялось


async def test_a_valid_invite_asks_for_the_name(ivan: Person, world: World) -> None:
    await ivan.start()

    assert ivan.last == t(K.ask_name)
    assert world.panel.routes() == [BY_TELEGRAM, CHECK]  # сначала «не зарегистрирован ли уже», потом приглашение
    assert world.panel.bodies(CHECK) == [{"code": CODE}]


@pytest.mark.parametrize(
    ("status", "key"),
    [
        ("used", K.invite_used),
        ("expired", K.invite_expired),
        ("revoked", K.invite_revoked),
        ("unknown", K.invite_unknown),
    ],
)
async def test_every_invite_problem_gets_its_own_text_and_ends_the_dialog(world: World, status: str, key: str) -> None:
    if status != "unknown":
        world.panel.add_invite(CODE, status)
    ivan = world.person()

    await ivan.start()
    assert ivan.last == t(key)

    await ivan.say(NAME)  # диалога нет: имя никто не ждёт
    assert ivan.last == t(K.unknown_input)
    assert world.panel.count(REGISTER) == 0


async def test_a_used_code_is_not_given_to_a_second_person(world: World) -> None:
    world.panel.add_invite(CODE)
    first, second = world.person(), world.person(OTHER_TG_ID, "maria")
    await first.register()

    await second.start()

    assert second.last == t(K.invite_used)


async def test_messages_in_a_group_chat_are_ignored(world: World) -> None:
    world.panel.add_invite(CODE)
    in_group = world.person(chat_type="supergroup")

    await in_group.start()
    await in_group.say("/status")
    await in_group.say("/help")

    assert in_group.texts == [] and world.panel.calls == []


async def test_start_in_the_middle_begins_the_dialog_anew(ivan: Person, world: World) -> None:
    world.panel.add_invite(OTHER_CODE)
    await ivan.start()
    await ivan.say(NAME)

    await ivan.start(OTHER_CODE)
    assert ivan.last == t(K.ask_name)
    await ivan.say(NAME)
    await ivan.say(EMAIL)
    await ivan.press(K.btn_yes)

    (body,) = posted(world)
    assert body["code"] == OTHER_CODE


async def test_panel_down_at_the_start_is_said_plainly_and_the_dialog_can_be_started_again(
    ivan: Person, world: World
) -> None:
    world.panel.override(CHECK, unavailable(), times=10)

    await ivan.start()
    assert ivan.last == t(K.panel_unavailable)

    world.panel.recover()
    await ivan.start()
    assert ivan.last == t(K.ask_name)


# --- имя -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Иван", "Иван Петров", "A", "x" * 64, "Ёлка-Палка О'Брайен", "李雷", "Иван 🙂", "<b>Иван</b> & <i>Co</i>"]
)
async def test_a_reasonable_name_is_accepted(ivan: Person, name: str) -> None:
    await ivan.start()

    await ivan.say(name)

    assert ivan.last == t(K.ask_email)


@pytest.mark.parametrize(
    "name",
    [
        "   ",
        "x" * 65,
        "Иван\nПетров",
        "Иван\r\nПетров",
        "Иван\tПетров",
        "Иван‮Петров",  # RLO: подмена текста на экране админа
        "Иван​Петров",  # нулевая ширина
        "Иван Петров",  # разделитель строк
        "Иван Петров",  # разделитель абзацев
        "Ив\x00ан",
        "Ив\x1ban",
    ],
    ids=["blank", "65 chars", "LF", "CRLF", "tab", "RLO", "zero width", "LS", "PS", "NUL", "ESC"],
)
async def test_a_name_with_control_or_invisible_characters_or_the_wrong_length_is_asked_again(
    ivan: Person, name: str
) -> None:
    await ivan.start()

    await ivan.say(name)
    assert ivan.last == t(K.bad_name)

    await ivan.say(NAME)  # диалог остался на шаге «имя»
    assert ivan.last == t(K.ask_email)


async def test_the_name_is_trimmed(ivan: Person, world: World) -> None:
    await ivan.start()
    await ivan.say("   Иван   ")
    await ivan.say(EMAIL)

    assert ivan.last == t(K.confirm, name="Иван", email=EMAIL)
    await ivan.press(K.btn_yes)
    assert posted(world)[0]["name"] == "Иван"


async def test_a_sticker_instead_of_the_name_is_answered_and_the_step_stays(ivan: Person) -> None:
    await ivan.start()

    texts = len(ivan.texts)
    await ivan.send_sticker()

    assert len(ivan.texts) > texts
    await ivan.say(NAME)
    assert ivan.last == t(K.ask_email)


# --- почта -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("email", [EMAIL, "ivan+vpn@mail.example.org", "Ivan.Petrov@Example.ORG", "a@b.co"])
async def test_a_normal_email_is_accepted_as_typed(ivan: Person, world: World, email: str) -> None:
    await ivan.start()
    await ivan.say(NAME)

    await ivan.say(email)

    assert ivan.last == t(K.confirm, name=NAME, email=email)
    await ivan.press(K.btn_yes)
    assert posted(world)[0]["email"] == email


@pytest.mark.parametrize(
    "email", ["ivan", "ivan@", "@example.org", "iv an@example.org", "a@b@c.org", "ivan@@example.org"]
)
async def test_a_malformed_email_is_asked_again(ivan: Person, email: str) -> None:
    await ivan.start()
    await ivan.say(NAME)

    await ivan.say(email)
    assert ivan.last == t(K.bad_email)

    await ivan.say(EMAIL)
    assert ivan.last == t(K.confirm, name=NAME, email=EMAIL)


# --- подтверждение ---------------------------------------------------------------------------------------------------


async def test_confirmation_shows_the_data_and_two_buttons(ivan: Person) -> None:
    await ivan.to_confirm()

    assert ivan.buttons() == [t(K.btn_yes), t(K.btn_edit)]


async def test_names_with_markup_are_shown_as_plain_text(ivan: Person, world: World) -> None:
    """parse_mode не задан: «<b>» в имени остаётся буквами, а не превращается в разметку (инъекция HTML)."""
    hostile = '<b>Иван</b> <a href="https://evil.example">клик</a>'

    await ivan.to_confirm(name=hostile)

    assert hostile in ivan.last
    assert world.telegram.parse_modes() == {None}


async def test_the_bot_never_uses_a_parse_mode(ivan: Person, world: World) -> None:
    await ivan.register()
    await ivan.say("/status")
    await ivan.say("/help")

    assert world.telegram.parse_modes() == {None}


async def test_edit_returns_to_the_name_and_the_old_buttons_go_away(ivan: Person, world: World) -> None:
    await ivan.to_confirm()

    await ivan.press(K.btn_edit)
    assert ivan.last == t(K.ask_name) and ivan.buttons() == []

    await ivan.say("Пётр")
    await ivan.say(OTHER_EMAIL)
    assert ivan.last == t(K.confirm, name="Пётр", email=OTHER_EMAIL)
    await ivan.press(K.btn_yes)

    (body,) = posted(world)
    assert (body["name"], body["email"], body["code"]) == ("Пётр", OTHER_EMAIL, CODE)


# --- отправка --------------------------------------------------------------------------------------------------------


async def test_yes_sends_the_registration_and_says_it_is_being_prepared(ivan: Person, world: World) -> None:
    await ivan.to_confirm()

    await ivan.press(K.btn_yes)

    assert posted(world) == [
        {"code": CODE, "telegram_id": TG_ID, "telegram_username": "ivan_p", "name": NAME, "email": EMAIL}
    ]
    assert t(K.preparing) in ivan.texts
    assert ivan.buttons() == []
    assert world.telegram.answers, "the button press must be answered, otherwise the Telegram client keeps spinning"


async def test_a_person_without_a_telegram_username_is_registered_too(world: World) -> None:
    world.panel.add_invite(CODE)
    nameless = world.person(username=None)

    await nameless.register()

    assert posted(world)[0].get("telegram_username") is None


async def test_the_registration_is_remembered_for_the_watcher_and_the_dialog_state_is_wiped(
    ivan: Person, world: World
) -> None:
    await ivan.register()

    assert await watch_ids(world.storage) == [world.panel.only_registration()]
    key = ivan.storage_key()
    assert await world.storage.get_state(key) is None and await world.storage.get_data(key) == {}


async def test_name_and_email_do_not_stay_on_disk_after_the_registration(ivan: Person, world: World) -> None:
    await ivan.register()

    connection = sqlite3.connect(world.state_file)
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert EMAIL not in dump and NAME not in dump and CODE not in dump


async def test_buttons_are_taken_away_before_the_panel_is_asked(ivan: Person, world: World) -> None:
    hold = world.panel.hold(REGISTER)
    await ivan.to_confirm()

    pressing = asyncio.create_task(ivan.press(K.btn_yes))
    await wait_until(lambda: hold.requests == 1, what="the registration request")
    try:
        assert ivan.buttons() == []  # пока панель думает, нажимать уже нечего
    finally:
        hold.release()
        await pressing


async def test_a_second_yes_while_the_first_is_in_flight_sends_one_registration(ivan: Person, world: World) -> None:
    hold = world.panel.hold(REGISTER)
    await ivan.to_confirm()
    yes = next(button for button in world.telegram.live_buttons(ivan.id) if button.text == t(K.btn_yes))

    first = asyncio.create_task(ivan.press_button(yes))
    await wait_until(lambda: hold.requests == 1, what="the first registration request")
    second = asyncio.create_task(ivan.press_button(yes))  # человек нетерпеливо нажал ещё раз: кнопка у него ещё видна
    await settle()  # второе нажатие доходит, докуда пустит замок
    hold.release()
    await asyncio.gather(first, second)

    assert world.panel.count(REGISTER) == 1
    assert len(world.panel.registrations) == 1


async def test_two_simultaneous_yes_presses_send_one_registration(ivan: Person, world: World) -> None:
    """Порядок обработки двух нажатий может быть любым - POST всё равно один (замок на пользователя)."""
    await ivan.to_confirm()
    yes = next(button for button in world.telegram.live_buttons(ivan.id) if button.text == t(K.btn_yes))

    await asyncio.gather(ivan.press_button(yes), ivan.press_button(yes), ivan.press_button(yes))

    assert world.panel.count(REGISTER) == 1


async def test_an_old_yes_button_after_the_registration_does_nothing(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    yes = next(button for button in world.telegram.live_buttons(ivan.id) if button.text == t(K.btn_yes))
    await ivan.press_button(yes)

    answers = len(world.telegram.answers)
    await ivan.press_button(yes)

    assert world.panel.count(REGISTER) == 1
    assert len(world.telegram.answers) > answers  # нажатие всё равно подтверждено


async def test_the_lock_is_per_person_not_global(world: World) -> None:
    world.panel.add_invite(CODE)
    world.panel.add_invite(OTHER_CODE)
    first, second = world.person(), world.person(OTHER_TG_ID, "maria")
    await first.to_confirm()
    await second.to_confirm(OTHER_CODE, "Мария", OTHER_EMAIL)
    hold = world.panel.hold(REGISTER)

    pressing = [asyncio.create_task(first.press(K.btn_yes)), asyncio.create_task(second.press(K.btn_yes))]
    try:
        await wait_until(lambda: hold.requests == 2, what="both registration requests at once")
    finally:
        hold.release()
        await asyncio.gather(*pressing)

    assert len(world.panel.registrations) == 2


# --- отказы панели при отправке --------------------------------------------------------------------------------------


async def test_a_taken_email_goes_back_to_the_email_step_and_keeps_the_rest(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, conflict("email_taken"))

    await ivan.press(K.btn_yes)
    assert t(K.email_taken) in ivan.texts[-2:]

    await ivan.say(OTHER_EMAIL)
    assert ivan.last == t(K.confirm, name=NAME, email=OTHER_EMAIL)  # имя не спрашивали заново
    await ivan.press(K.btn_yes)
    first, second = posted(world)
    assert (first["code"], first["name"]) == (second["code"], second["name"]) == (CODE, NAME)
    assert second["email"] == OTHER_EMAIL


async def test_a_taken_email_found_by_the_real_rules_of_the_panel(ivan: Person, world: World) -> None:
    world.panel.add_invite(OTHER_CODE)
    other = world.person(OTHER_TG_ID, "maria")
    await other.register(OTHER_CODE, "Мария", EMAIL.upper())  # та же почта в другом регистре
    await ivan.to_confirm()

    await ivan.press(K.btn_yes)

    assert t(K.email_taken) in ivan.texts[-2:]


async def test_a_taken_telegram_account_means_already_registered(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, conflict("telegram_taken"))

    await ivan.press(K.btn_yes)

    assert ivan.last == t(K.already_registered)
    await ivan.say(NAME)
    assert ivan.last == t(K.unknown_input)  # диалог закончился


@pytest.mark.parametrize(
    ("reason", "keys"),
    [
        ("invite_used", [K.invite_used]),
        ("invite_expired", [K.invite_expired]),
        ("invite_invalid", [K.invite_unknown, K.invite_revoked, K.bad_link]),
    ],
)
async def test_an_invite_that_stopped_being_valid_ends_the_dialog(
    ivan: Person, world: World, reason: str, keys: list[str]
) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, conflict(reason))

    await ivan.press(K.btn_yes)

    assert ivan.last in [t(key) for key in keys]
    await ivan.say(NAME)
    assert ivan.last == t(K.unknown_input)


async def test_the_invite_taken_by_someone_else_in_the_meantime_is_reported(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    world.panel.invites[CODE] = "used"  # пока человек вводил данные, код занял другой

    await ivan.press(K.btn_yes)

    assert ivan.last == t(K.invite_used)


@pytest.mark.parametrize("reason", ["conflict", "something_new"])
async def test_an_unexpected_conflict_is_still_answered(ivan: Person, world: World, reason: str) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, conflict(reason))
    before = len(ivan.texts)

    await ivan.press(K.btn_yes)

    assert len(ivan.texts) > before
    assert world.telegram.answers


async def test_a_panel_outage_keeps_the_confirmation_and_yes_works_again(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, unavailable(), times=10)

    await ivan.press(K.btn_yes)
    assert ivan.last == t(K.panel_unavailable)
    assert t(K.btn_yes) in ivan.buttons()  # остались в «подтверждении»

    world.panel.recover()
    await ivan.press(K.btn_yes)

    assert t(K.preparing) in ivan.texts
    assert len(world.panel.registrations) == 1
    first, second = posted(world)[0], posted(world)[-1]
    assert first == second  # повтор ушёл с теми же данными


async def test_a_rejected_name_goes_back_to_the_name_step(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, rejected("name", echo=NAME))

    await ivan.press(K.btn_yes)
    assert ivan.last == t(K.bad_name)

    await ivan.say("Пётр")
    assert ivan.last == t(K.ask_email)
    await ivan.say(EMAIL)
    assert ivan.last == t(K.confirm, name="Пётр", email=EMAIL)


async def test_a_rejected_email_goes_back_to_the_email_step(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, rejected("email", echo=EMAIL))

    await ivan.press(K.btn_yes)
    assert ivan.last == t(K.bad_email)

    await ivan.say(OTHER_EMAIL)
    assert ivan.last == t(K.confirm, name=NAME, email=OTHER_EMAIL)


async def test_a_rate_limit_is_explained_and_yes_can_be_pressed_later(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, rate_limited(60))

    await ivan.press(K.btn_yes)
    assert ivan.last == t(K.rate_limited) and t(K.btn_yes) in ivan.buttons()

    await ivan.press(K.btn_yes)
    assert t(K.preparing) in ivan.texts


async def test_a_rejected_key_is_not_explained_to_the_person(ivan: Person, world: World) -> None:
    """Про ключ панели человеку знать незачем (это дело админа и лога): он видит то же, что при недоступной панели."""
    await ivan.to_confirm()
    world.panel.override(REGISTER, unauthorized())

    await ivan.press(K.btn_yes)

    assert ivan.last == t(K.panel_unavailable)


# --- устойчивость ----------------------------------------------------------------------------------------------------


async def test_the_dialog_survives_a_restart_of_the_bot(ivan: Person, world: World) -> None:
    await ivan.start()
    await ivan.say(NAME)

    await world.restart()
    await ivan.say(EMAIL)

    assert ivan.last == t(K.confirm, name=NAME, email=EMAIL)
    await ivan.press(K.btn_yes)
    assert posted(world)[0]["code"] == CODE


async def test_a_confirmation_pressed_after_a_restart_still_works(ivan: Person, world: World) -> None:
    await ivan.to_confirm()

    await world.restart()
    await ivan.press(K.btn_yes)

    assert len(world.panel.registrations) == 1


# --- /status, /help, прочее ------------------------------------------------------------------------------------------


async def test_status_shows_name_date_and_protocol(world: World) -> None:
    world.panel.add_user(TG_ID, NAME)
    ivan = world.person()

    await ivan.say("/status")

    assert NAME in ivan.last and FAKE_UNTIL in ivan.last and "amneziawg" in ivan.last.lower()
    assert world.panel.routes() == [BY_TELEGRAM] and world.panel.calls[0].path.endswith(f"/{TG_ID}")


async def test_status_without_a_protocol_does_not_print_none(world: World) -> None:
    world.panel.add_user(TG_ID, NAME)
    world.panel.users[TG_ID]["protocol"] = None
    ivan = world.person()

    await ivan.say("/status")

    assert NAME in ivan.last and FAKE_UNTIL in ivan.last and "None" not in ivan.last and "null" not in ivan.last


async def test_status_of_a_stranger_says_not_registered(world: World) -> None:
    ivan = world.person()

    await ivan.say("/status")

    assert ivan.last == t(K.not_registered)


async def test_status_when_the_panel_is_down(world: World) -> None:
    world.panel.override(BY_TELEGRAM, unavailable(), times=10)
    ivan = world.person()

    await ivan.say("/status")

    assert ivan.last == t(K.panel_unavailable)


async def test_status_with_a_revoked_key_does_not_crash(world: World) -> None:
    world.panel.override(BY_TELEGRAM, unauthorized())
    ivan = world.person()

    await ivan.say("/status")

    assert ivan.last == t(K.panel_unavailable)


async def test_help_explains_the_bot_without_asking_the_panel(world: World) -> None:
    ivan = world.person()

    await ivan.say("/help")

    assert ivan.last == t(K.help)
    assert world.panel.calls == []


@pytest.mark.parametrize("text", ["привет", "что ты умеешь?", "/frobnicate"])
async def test_anything_else_gets_a_short_hint_and_never_reaches_the_panel(world: World, text: str) -> None:
    ivan = world.person()

    await ivan.say(text)

    assert ivan.last == t(K.unknown_input)
    assert world.panel.calls == []


async def test_nobody_else_sees_a_conversation(world: World) -> None:
    world.panel.add_invite(CODE)
    ivan, maria = world.person(), world.person(OTHER_TG_ID, "maria")

    await ivan.start()
    await ivan.say(NAME)
    await maria.say(EMAIL)  # у Марии диалога нет: чужая почта в чужой диалог не попадает

    assert maria.last == t(K.unknown_input)
    assert ivan.last == t(K.ask_email)
