"""Привязка Telegram админа (этап 25.5, Р11): `/admin <код>` и `/start <код в 32 символа>`.

Код привязки - 32 символа, приглашение - 24: `/start` различает их по длине. Привязка сбрасывает диалог регистрации, код
не попадает ни в лог, ни в ответы бота; в группах команда не работает (код увидели бы все)."""

import io
from pathlib import Path

from fakes import conflict, rate_limited, unavailable
from support import (
    ADMIN_CODE,
    CODE,
    INVITE_24,
    K,
    World,
    running_world,
    setup_logging,
    t,
)

LINK = "POST /admin-links"
CHECK = "POST /invites/check"


async def test_admin_command_links_the_telegram_and_names_the_admin(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE, "Анна")
    person = world.person(username="anna_tg")

    await person.say(f"/admin {ADMIN_CODE}")

    assert world.panel.admin_links == [{"code": ADMIN_CODE, "telegram_id": person.id, "telegram_username": "anna_tg"}]
    assert person.last == t(K.admin_linked, name="Анна")


async def test_admin_command_without_a_username_still_links(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE)
    person = world.person(username=None)

    await person.say(f"/admin {ADMIN_CODE}")

    assert world.panel.admin_links[0]["telegram_username"] is None
    assert person.last == t(K.admin_linked, name="Анна")


async def test_start_with_a_32_character_code_links_an_admin_and_never_checks_an_invite(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE)
    person = world.person()

    await person.say(f"/start {ADMIN_CODE}")

    assert len(world.panel.admin_links) == 1 and world.panel.count(CHECK) == 0
    assert person.last == t(K.admin_linked, name="Анна")


async def test_start_with_a_24_character_code_is_an_invite_and_never_a_link(world: World) -> None:
    world.panel.add_invite(INVITE_24)
    person = world.person()

    await person.start(INVITE_24)

    assert world.panel.count(CHECK) == 1 and world.panel.count(LINK) == 0
    assert person.last == t(K.ask_name)


async def test_a_normal_invite_code_still_works(world: World) -> None:
    world.panel.add_invite(CODE)
    person = world.person()

    await person.start()

    assert person.last == t(K.ask_name) and world.panel.count(LINK) == 0


async def test_admin_command_without_a_code_explains_the_usage_and_asks_nothing(world: World) -> None:
    person = world.person()

    await person.say("/admin")

    assert person.last == t(K.admin_link_usage) and world.panel.count(LINK) == 0


async def test_an_unknown_code_is_reported_as_invalid(world: World) -> None:
    person = world.person()

    await person.say(f"/admin {ADMIN_CODE}")

    assert person.last == t(K.admin_link_invalid) and world.panel.admin_links == []


async def test_a_used_code_is_reported_as_invalid_the_second_time(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE)
    person = world.person()
    await person.say(f"/admin {ADMIN_CODE}")

    await person.say(f"/admin {ADMIN_CODE}")

    assert person.last == t(K.admin_link_invalid) and len(world.panel.admin_links) == 1


async def test_an_expired_code_has_its_own_text(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE, refusal="link_expired")
    person = world.person()

    await person.say(f"/admin {ADMIN_CODE}")

    assert person.last == t(K.admin_link_expired)


async def test_a_taken_telegram_has_its_own_text(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE, refusal="telegram_taken")
    person = world.person()

    await person.say(f"/admin {ADMIN_CODE}")

    assert person.last == t(K.admin_link_taken)


async def test_a_dead_panel_gets_the_usual_apology(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE)
    world.panel.override(LINK, unavailable(), times=20)
    person = world.person()

    await person.say(f"/admin {ADMIN_CODE}")

    assert person.last == t(K.panel_unavailable)


async def test_rate_limiting_gets_the_usual_text(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE)
    world.panel.override(LINK, rate_limited(2))
    person = world.person()

    await person.say(f"/admin {ADMIN_CODE}")

    assert person.last in (t(K.rate_limited), t(K.panel_unavailable))


async def test_a_conflict_without_a_reason_is_not_a_crash(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE)
    world.panel.override(LINK, conflict(None))
    person = world.person()

    await person.say(f"/admin {ADMIN_CODE}")

    assert person.last in (t(K.admin_link_invalid), t(K.panel_unavailable))


async def test_a_malformed_code_is_not_even_sent_to_the_panel(world: World) -> None:
    person = world.person()

    for bad in ("<script>alert(1)</script>", "x" * 200, "кириллица-вместо-кода-1234567890"):
        await person.say(f"/admin {bad}")

    assert world.panel.count(LINK) == 0
    assert all(text in (t(K.admin_link_usage), t(K.admin_link_invalid)) for text in person.texts)


async def test_linking_drops_a_registration_in_progress(world: World) -> None:
    world.panel.add_invite(CODE)
    world.panel.add_admin_code(ADMIN_CODE)
    person = world.person()
    await person.start()
    assert person.last == t(K.ask_name)

    await person.say(f"/admin {ADMIN_CODE}")
    await person.say("Иван Петров")

    assert person.last == t(K.unknown_input)  # имя больше никто не ждёт
    assert world.panel.count("POST /registrations") == 0


async def test_the_reply_never_repeats_the_code(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE)
    person = world.person()

    await person.say(f"/admin {ADMIN_CODE}")
    await person.say(f"/admin {ADMIN_CODE}")

    assert all(ADMIN_CODE not in text for text in person.texts)


async def test_in_a_group_the_command_is_ignored_so_the_code_stays_unspent(world: World) -> None:
    world.panel.add_admin_code(ADMIN_CODE)
    person = world.person(chat_type="supergroup")

    await person.say(f"/admin {ADMIN_CODE}")

    assert world.panel.count(LINK) == 0 and person.texts == []


async def test_the_code_reaches_no_log_line(tmp_path: Path) -> None:
    stream = io.StringIO()
    async with running_world(tmp_path, level="DEBUG") as world:
        setup_logging(world.settings, stream)
        world.panel.add_admin_code(ADMIN_CODE)
        other = "Other" + "z" * 27
        person = world.person()

        await person.say(f"/admin {ADMIN_CODE}")
        await person.say(f"/start {other}")
        await person.say(f"/admin {other}")

    logged = stream.getvalue()
    assert ADMIN_CODE not in logged and other not in logged and "Анна" not in logged
