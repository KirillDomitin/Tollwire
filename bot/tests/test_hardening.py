"""Устойчивость бота после ревью этапа 25.2: лимит на человека, очистка просроченного состояния, ответы панели не по
контракту, зависшее состояние «отправляется», запуск с неверными настройками."""

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from fakes import FakeClock, FakePanel, Outcome
from support import CODE, EMAIL, NAME, PANEL_URL, SECRET_KEY, TG_ID, K, Person, World, make_storage, t
from tollwire_bot.panel import PanelClient, PanelUnexpected
from tollwire_bot.storage import DAY

REGISTER = "POST /registrations"


@pytest.fixture
def ivan(world: World) -> Person:
    world.panel.add_invite(CODE)
    return world.person()


def body(content: str, status: int = 200) -> Outcome:
    return lambda: httpx.Response(status, content=content, headers={"content-type": "application/json"})


# --- лимит на человека -----------------------------------------------------------------------------------------------


async def test_one_person_cannot_use_up_more_than_sixty_events_a_minute(world: World) -> None:
    flooder = world.person()

    for _ in range(80):
        await flooder.say("/help")

    assert flooder.texts.count(t(K.help)) == 60
    assert flooder.texts.count(t(K.rate_limited)) == 1  # об ограничении сказано один раз, дальше тишина


async def test_the_limit_is_per_person_and_the_panel_is_not_asked_for_throttled_events(world: World) -> None:
    flooder, calm = world.person(), world.person(TG_ID + 1, "maria")
    for _ in range(61):
        await flooder.say("/status")
    asked = world.panel.count("GET /users/by-telegram/*")

    await flooder.say("/status")
    await calm.say("/help")

    assert world.panel.count("GET /users/by-telegram/*") == asked == 60
    assert calm.last == t(K.help)


async def test_the_limit_is_lifted_after_the_window(world: World) -> None:
    person = world.person()
    for _ in range(61):
        await person.say("/help")

    world.clock.advance(61)
    await person.say("/help")

    assert person.last == t(K.help)


# --- очистка просроченного -------------------------------------------------------------------------------------------


async def test_purge_removes_expired_records_from_the_file_without_a_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "state.sqlite3"
    storage = make_storage(path, clock)
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=42, chat_id=1, user_id=1)
    await storage.set_data(key, {"email": "abandoned.person@example.org"})
    await storage.add_watch("reg-1", 1, 1, started_at=clock.wall())

    clock.advance(DAY + 60)
    await storage.purge()
    await storage.close()

    connection = sqlite3.connect(path)
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert "abandoned.person" not in dump and "reg-1" not in dump


async def test_any_write_also_clears_the_expired_records_of_others(tmp_path: Path) -> None:
    from aiogram.fsm.storage.base import StorageKey

    clock = FakeClock()
    path = tmp_path / "state.sqlite3"
    storage = make_storage(path, clock)
    await storage.set_data(StorageKey(bot_id=42, chat_id=1, user_id=1), {"email": "abandoned.person@example.org"})
    clock.advance(DAY + 60)

    await storage.set_data(StorageKey(bot_id=42, chat_id=2, user_id=2), {"name": "Другой"})
    await storage.close()

    connection = sqlite3.connect(path)
    try:
        assert "abandoned.person" not in "\n".join(connection.iterdump())
    finally:
        connection.close()


# --- ответы панели не по контракту -----------------------------------------------------------------------------------


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel(FakeClock())


@pytest.fixture
async def client(panel: FakePanel) -> PanelClient:
    async def nap(delay: float) -> None:
        return None

    return PanelClient(PANEL_URL, panel.key, transport=panel.transport, sleep=nap)


@pytest.mark.parametrize("content", ["<html>proxy error</html>", "", "[]", "null", '"text"'])
@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.check_invite("code"),
        lambda client: client.register(code="c", telegram_id=1, telegram_username=None, name="n", email="a@b.co"),
        lambda client: client.registration("00000000-0000-4000-8000-000000000000"),
        lambda client: client.user_by_telegram(1),
    ],
    ids=["check_invite", "register", "registration", "user_by_telegram"],
)
async def test_a_success_answer_that_is_not_the_contract_is_panel_unexpected(
    panel: FakePanel, client: PanelClient, content: str, call: Callable[[PanelClient], object]
) -> None:
    for route in ("POST /invites/check", REGISTER, "GET /registrations/*", "GET /users/by-telegram/*"):
        panel.override(route, body(content), times=5)

    with pytest.raises(PanelUnexpected):
        await call(client)  # type: ignore[misc]


async def test_an_answer_without_the_expected_fields_is_panel_unexpected(panel: FakePanel, client: PanelClient) -> None:
    panel.override("POST /invites/check", body('{"something": "else"}'))
    panel.override("GET /users/by-telegram/*", body('{"valid_until": "2026-11-25T12:00:00Z"}'))

    with pytest.raises(PanelUnexpected):
        await client.check_invite("code")
    with pytest.raises(PanelUnexpected):
        await client.user_by_telegram(1)


@pytest.mark.skipif(not shutil.which("openssl"), reason="нужен openssl")
async def test_the_client_does_not_demand_strict_x509_from_a_pinned_certificate(tmp_path: Path) -> None:
    """Сертификат закреплён по отпечатку при установке: придирки Python 3.13+ к профилю X.509 бота не отрезают."""
    import ssl

    from test_panel_client import issue_certificate
    from tollwire_bot.panel import _ssl_context

    cert, _ = issue_certificate(tmp_path)

    assert _ssl_context(str(cert)).verify_flags & ssl.VERIFY_X509_STRICT == 0
    assert _ssl_context(None).verify_flags == ssl.create_default_context().verify_flags


# --- человек не застревает -------------------------------------------------------------------------------------------


async def test_a_garbage_answer_to_the_registration_returns_to_the_confirmation(ivan: Person, world: World) -> None:
    await ivan.to_confirm()
    world.panel.override(REGISTER, body("<html>502 from a proxy</html>"))

    await ivan.press(K.btn_yes)

    assert ivan.last == t(K.panel_unavailable) and t(K.btn_yes) in ivan.buttons()
    await ivan.press(K.btn_yes)  # панель починилась
    assert t(K.preparing) in ivan.texts


async def test_a_failing_storage_after_the_registration_does_not_leave_the_person_stuck(
    ivan: Person, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ivan.to_confirm()

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk is full")

    monkeypatch.setattr(world.storage, "add_watch", broken)
    await ivan.press(K.btn_yes)
    assert ivan.last == t(K.panel_unavailable) and t(K.btn_yes) in ivan.buttons()

    monkeypatch.undo()
    await ivan.press(K.btn_yes)  # тот же запрос: панель вернёт ту же регистрацию
    assert len(world.panel.registrations) == 1 and t(K.preparing) in ivan.texts


async def test_a_message_in_the_submitting_state_brings_back_the_confirmation(ivan: Person, world: World) -> None:
    """Бота перезапустили посреди отправки: в хранилище осталось «отправляется». Любое сообщение возвращает кнопки."""
    from tollwire_bot.dialog import Registration

    await ivan.to_confirm()
    await world.storage.set_state(ivan.storage_key(), Registration.submitting)

    await ivan.say("ну что там?")

    assert ivan.last == t(K.confirm, name=NAME, email=EMAIL) and t(K.btn_yes) in ivan.buttons()
    await ivan.press(K.btn_yes)
    assert len(world.panel.registrations) == 1


async def test_a_submitting_state_without_data_is_cleared(ivan: Person, world: World) -> None:
    from tollwire_bot.dialog import Registration

    await world.storage.set_state(ivan.storage_key(), Registration.submitting)

    await ivan.say("привет")

    assert ivan.last == t(K.unknown_input)
    assert await world.storage.get_state(ivan.storage_key()) is None


# --- запуск ----------------------------------------------------------------------------------------------------------


def test_a_missing_setting_is_named_without_a_traceback_or_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tollwire_bot.__main__ import main

    monkeypatch.setenv("PANEL_URL", "https://panel.example.test")
    monkeypatch.setenv("BOT_API_KEY", SECRET_KEY)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(Path(__file__).parent)  # без .env рядом

    with pytest.raises(SystemExit) as exit_:
        main()

    message = str(exit_.value)
    assert "TELEGRAM_BOT_TOKEN" in message and SECRET_KEY not in message and "Traceback" not in message
