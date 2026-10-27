"""Команда /config (этап 25.3): новая ссылка на страницу конфига, прежняя гаснет.

Бот спрашивает панель (`POST .../config-link`) и сам ничего не решает: текст зависит от `reason` ответа 409. Ссылка в
чат - только адрес страницы, без конфига и ключей."""

import pytest

from fakes import CONFIG_LINK, conflict, rate_limited, unauthorized, unavailable
from support import CODE, OTHER_TG_ID, TG_ID, K, Person, World, settle, t

REFUSALS = [
    ("config_unavailable", K.config_unavailable),
    ("no_access", K.config_no_access),
    ("not_ready", K.config_not_ready),
    ("not_configured", K.config_not_configured),
]


@pytest.fixture
def ivan(world: World) -> Person:
    world.panel.add_user(TG_ID)
    return world.person()


async def test_config_gives_a_link_to_the_page(ivan: Person, world: World) -> None:
    await ivan.say("/config")

    (url,) = ivan.links()
    assert world.panel.live_links(TG_ID)[0].url == url
    assert any(url in text for text in ivan.texts) and "[Interface]" not in "".join(ivan.texts)
    assert url in ivan.last


async def test_config_asks_the_panel_for_exactly_this_person(ivan: Person, world: World) -> None:
    world.panel.add_user(OTHER_TG_ID, "Мария")

    await ivan.say("/config")

    (call,) = [call for call in world.panel.calls if call.route == CONFIG_LINK]
    assert call.path.endswith(f"/users/by-telegram/{TG_ID}/config-link") and call.method == "POST"
    assert world.panel.live_links(OTHER_TG_ID) == []


async def test_a_second_config_gives_a_new_link_and_kills_the_old_one(ivan: Person, world: World) -> None:
    await ivan.say("/config")
    await ivan.say("/config")

    first, second = ivan.links()
    assert first != second
    assert [link.live for link in world.panel.links] == [False, True]
    assert world.panel.live_links(TG_ID)[0].url == second


async def test_config_of_an_unregistered_person_says_so(world: World) -> None:
    stranger = world.person(OTHER_TG_ID, "stranger")

    await stranger.say("/config")

    assert stranger.last == t(K.not_registered) and not stranger.links()
    assert world.panel.links == []


@pytest.mark.parametrize(("reason", "key"), REFUSALS)
async def test_every_refusal_of_the_panel_has_its_own_answer_and_no_link(
    ivan: Person, world: World, reason: str, key: str
) -> None:
    world.panel.link_refusal[TG_ID] = reason

    await ivan.say("/config")

    assert ivan.texts == [t(key)] and not ivan.links()


async def test_the_refusal_texts_are_all_different() -> None:
    texts = [t(key) for _, key in REFUSALS]
    assert len(set(texts)) == len(texts)


async def test_after_a_refusal_the_next_config_works_when_the_panel_agrees(ivan: Person, world: World) -> None:
    world.panel.link_refusal[TG_ID] = "not_ready"
    await ivan.say("/config")
    del world.panel.link_refusal[TG_ID]

    await ivan.say("/config")

    assert len(ivan.links()) == 1 and len(world.panel.links) == 1


async def test_an_unavailable_panel_gets_the_ordinary_answer(ivan: Person, world: World) -> None:
    world.panel.override(CONFIG_LINK, unavailable(), times=5)

    await ivan.say("/config")

    assert ivan.last == t(K.panel_unavailable) and not ivan.links()


async def test_a_rate_limited_panel_gets_the_ordinary_answer(ivan: Person, world: World) -> None:
    world.panel.override(CONFIG_LINK, rate_limited(60), times=2)

    await ivan.say("/config")

    assert ivan.last == t(K.rate_limited) and not ivan.links()


async def test_a_rejected_key_does_not_crash_the_command(ivan: Person, world: World) -> None:
    world.panel.override(CONFIG_LINK, unauthorized())

    await ivan.say("/config")

    assert ivan.last == t(K.panel_unavailable) and not ivan.links()


async def test_an_unknown_reason_is_handled_like_any_conflict(ivan: Person, world: World) -> None:
    world.panel.override(CONFIG_LINK, conflict(None))

    await ivan.say("/config")

    assert len(ivan.texts) == 1 and not ivan.links()  # человек получил ответ, а не тишину


async def test_config_works_in_the_middle_of_the_dialog_and_keeps_the_dialog(world: World) -> None:
    world.panel.add_invite(CODE)
    stranger = world.person(OTHER_TG_ID, "stranger")  # регистрируется, учётки ещё нет
    await stranger.start()

    await stranger.say("/config")

    assert stranger.last == t(K.not_registered)  # команда сработала, а не была принята за имя
    await stranger.say("Иван Петров")  # диалог продолжается с того места, где остановился
    assert stranger.last == t(K.ask_email)


async def test_config_is_silent_in_a_group(ivan: Person, world: World) -> None:
    group = world.person(TG_ID, "ivan_p", chat_type="supergroup")

    await group.say("/config")
    await settle()

    assert world.panel.count(CONFIG_LINK) == 0 and not ivan.texts


async def test_the_config_command_is_not_unknown_input(ivan: Person) -> None:
    await ivan.say("/config")

    assert t(K.unknown_input) not in ivan.texts
