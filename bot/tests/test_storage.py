"""SqliteStorage: FSM-состояние диалога и список ожидающих регистраций в одном файле тома /data.

Имя и почта человека лежат на диске, пока он не закончил диалог: файл 0600, запись живёт не дольше 24 часов, по
завершении диалога строка уходит. Время подменено (FakeClock.wall), настоящие часы не нужны."""

import asyncio
import os
import sqlite3
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from fakes import FakeClock
from support import add_watch, make_storage, watch_ids
from tollwire_bot.storage import SqliteStorage

DAY = 24 * 60 * 60
KEY = StorageKey(bot_id=42, chat_id=1001, user_id=1001)
OTHER = StorageKey(bot_id=42, chat_id=1002, user_id=1002)


class Flow(StatesGroup):
    name = State()
    email = State()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


@pytest.fixture
async def storage(path: Path, clock: FakeClock) -> AsyncIterator[SqliteStorage]:
    opened = make_storage(path, clock)
    yield opened
    await opened.close()


def raw_dump(path: Path) -> str:
    """Всё содержимое файла БД текстом (все таблицы): по нему видно, что на диске осталось."""
    connection = sqlite3.connect(path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


# --- контракт FSM ----------------------------------------------------------------------------------------------------


def test_it_is_an_aiogram_storage(storage: SqliteStorage) -> None:
    assert isinstance(storage, BaseStorage)


async def test_a_fresh_key_has_no_state_and_no_data(storage: SqliteStorage) -> None:
    assert await storage.get_state(KEY) is None
    assert await storage.get_data(KEY) == {}


async def test_state_is_stored_from_a_state_object_or_a_string(storage: SqliteStorage) -> None:
    await storage.set_state(KEY, Flow.name)
    assert await storage.get_state(KEY) == Flow.name.state

    await storage.set_state(KEY, "custom:confirm")
    assert await storage.get_state(KEY) == "custom:confirm"


async def test_state_can_be_cleared(storage: SqliteStorage) -> None:
    await storage.set_state(KEY, Flow.email)

    await storage.set_state(KEY, None)

    assert await storage.get_state(KEY) is None


async def test_data_round_trips_unicode_and_nesting(storage: SqliteStorage) -> None:
    data = {"name": "Иван Петров 🙂", "email": "ivan@example.org", "tries": 2, "nested": {"a": [1, 2, None]}}

    await storage.set_data(KEY, data)

    assert await storage.get_data(KEY) == data


async def test_set_data_replaces_and_update_data_merges(storage: SqliteStorage) -> None:
    await storage.set_data(KEY, {"code": "C", "name": "A"})
    merged = await storage.update_data(KEY, {"name": "B"})

    assert merged == {"code": "C", "name": "B"}
    assert await storage.get_data(KEY) == {"code": "C", "name": "B"}

    await storage.set_data(KEY, {"only": 1})
    assert await storage.get_data(KEY) == {"only": 1}


async def test_the_returned_data_is_a_copy(storage: SqliteStorage) -> None:
    await storage.set_data(KEY, {"name": "A"})

    (await storage.get_data(KEY))["name"] = "changed"

    assert await storage.get_data(KEY) == {"name": "A"}


async def test_people_do_not_share_state(storage: SqliteStorage) -> None:
    await storage.set_state(KEY, Flow.name)
    await storage.set_data(KEY, {"name": "Иван"})

    assert await storage.get_state(OTHER) is None and await storage.get_data(OTHER) == {}


async def test_the_same_person_in_another_chat_is_another_dialog(storage: SqliteStorage) -> None:
    group = StorageKey(bot_id=42, chat_id=-500, user_id=KEY.user_id)
    await storage.set_state(KEY, Flow.name)

    assert await storage.get_state(group) is None


async def test_parallel_writers_do_not_lose_updates(storage: SqliteStorage) -> None:
    people = [StorageKey(bot_id=42, chat_id=n, user_id=n) for n in range(1, 31)]

    await asyncio.gather(*(storage.set_data(person, {"n": person.user_id}) for person in people))

    assert [(await storage.get_data(person))["n"] for person in people] == [person.user_id for person in people]


async def test_close_can_be_called_twice(path: Path, clock: FakeClock) -> None:
    opened = make_storage(path, clock)
    await opened.set_state(KEY, Flow.name)

    await opened.close()
    await opened.close()


# --- файл ------------------------------------------------------------------------------------------------------------


async def test_state_survives_a_restart(path: Path, clock: FakeClock) -> None:
    first = make_storage(path, clock)
    await first.set_state(KEY, Flow.email)
    await first.set_data(KEY, {"name": "Иван", "code": "C"})
    await first.close()

    second = make_storage(path, clock)
    try:
        assert await second.get_state(KEY) == Flow.email.state
        assert await second.get_data(KEY) == {"name": "Иван", "code": "C"}
    finally:
        await second.close()


@pytest.mark.skipif(sys.platform == "win32", reason="права POSIX")
async def test_the_file_and_its_journal_are_private_under_a_normal_umask(path: Path, clock: FakeClock) -> None:
    """Имя и почта лежат в файле и в журнале SQLite (-wal): читать их должен только владелец."""
    previous = os.umask(0o022)  # обычная маска: без явных прав файл вышел бы 0644
    try:
        opened = make_storage(path, clock)
        await opened.set_data(KEY, {"email": "ivan@example.org"})
        try:
            files = sorted(path.parent.glob(path.name + "*"))
            assert path in files
            assert {stat.S_IMODE(file.stat().st_mode) for file in files} == {0o600}, [file.name for file in files]
        finally:
            await opened.close()
    finally:
        os.umask(previous)


async def test_finished_dialog_leaves_nothing_on_disk(path: Path, storage: SqliteStorage) -> None:
    await storage.set_state(KEY, Flow.email)
    await storage.set_data(KEY, {"name": "Иван Секретов", "email": "secret.person@example.org", "code": "CODE-X"})
    assert "secret.person@example.org" in raw_dump(path)

    await storage.set_state(KEY, None)
    await storage.set_data(KEY, {})

    dump = raw_dump(path)
    assert "secret.person" not in dump and "Иван Секретов" not in dump and "CODE-X" not in dump


# --- срок жизни ------------------------------------------------------------------------------------------------------


async def test_a_record_lives_for_a_day(storage: SqliteStorage, clock: FakeClock) -> None:
    await storage.set_state(KEY, Flow.name)
    await storage.set_data(KEY, {"name": "Иван"})

    clock.advance(DAY - 60)
    assert await storage.get_state(KEY) == Flow.name.state and await storage.get_data(KEY) == {"name": "Иван"}

    clock.advance(120)
    assert await storage.get_state(KEY) is None and await storage.get_data(KEY) == {}


async def test_a_new_write_extends_the_life_of_the_record(storage: SqliteStorage, clock: FakeClock) -> None:
    await storage.set_state(KEY, Flow.name)
    clock.advance(DAY - 60)

    await storage.update_data(KEY, {"name": "Иван"})  # человек всё ещё в диалоге
    clock.advance(DAY - 60)

    assert await storage.get_state(KEY) == Flow.name.state


async def test_expired_records_are_wiped_from_the_file_at_start(path: Path, clock: FakeClock) -> None:
    first = make_storage(path, clock)
    await first.set_state(KEY, Flow.email)
    await first.set_data(KEY, {"email": "stale.person@example.org"})
    await first.close()
    clock.advance(DAY + 60)

    second = make_storage(path, clock)
    try:
        await second.get_state(OTHER)  # любое обращение: инициализация могла быть ленивой
        assert "stale.person" not in raw_dump(path)
        assert await second.get_state(KEY) is None
    finally:
        await second.close()


async def test_a_custom_ttl_is_honoured(path: Path, clock: FakeClock) -> None:
    short = make_storage(path, clock, ttl=60)
    try:
        await short.set_state(KEY, Flow.name)
        clock.advance(61)
        assert await short.get_state(KEY) is None
    finally:
        await short.close()


# --- ожидающие регистрации (для watcher) -----------------------------------------------------------------------------


async def test_pending_registrations_are_listed_and_removed(storage: SqliteStorage) -> None:
    await add_watch(storage, "reg-1", 1001)
    await add_watch(storage, "reg-2", 1002)
    assert sorted(await watch_ids(storage)) == ["reg-1", "reg-2"]

    await storage.remove_watch("reg-1")

    assert await watch_ids(storage) == ["reg-2"]


async def test_a_pending_registration_is_added_once(storage: SqliteStorage) -> None:
    await add_watch(storage, "reg-1", 1001)
    await add_watch(storage, "reg-1", 1001)

    assert await watch_ids(storage) == ["reg-1"]


async def test_removing_an_unknown_registration_is_harmless(storage: SqliteStorage) -> None:
    await storage.remove_watch("never-added")

    assert await watch_ids(storage) == []


async def test_pending_registrations_survive_a_restart(path: Path, clock: FakeClock) -> None:
    first = make_storage(path, clock)
    await add_watch(first, "reg-1", 1001, started_at=clock.wall())
    await first.close()

    second = make_storage(path, clock)
    try:
        (watch,) = await second.watches()
        assert str(watch.registration_id) == "reg-1"
    finally:
        await second.close()


async def test_pending_registrations_expire_like_everything_else(storage: SqliteStorage, clock: FakeClock) -> None:
    await add_watch(storage, "reg-1", 1001, started_at=clock.wall())

    clock.advance(DAY + 60)

    assert await watch_ids(storage) == []


async def test_a_pending_registration_is_not_a_dialog(storage: SqliteStorage) -> None:
    await add_watch(storage, "reg-1", KEY.chat_id)

    assert await storage.get_state(KEY) is None and await storage.get_data(KEY) == {}
