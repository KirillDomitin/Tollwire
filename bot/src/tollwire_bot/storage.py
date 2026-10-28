"""Состояние бота в одном файле SQLite (том /data): FSM диалогов и список регистраций, за которыми бот следит.

Имя и почта человека лежат на диске, пока он не закончил диалог: файл и его журнал 0600, запись живёт не дольше
ttl (24 часа с последней записи), завершённый диалог не оставляет строки, secure_delete стирает и сами страницы."""

import asyncio
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

DAY = 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fsm (
    key        TEXT PRIMARY KEY,
    state      TEXT,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watches (
    registration_id TEXT PRIMARY KEY,
    chat_id         INTEGER NOT NULL,
    telegram_id     INTEGER NOT NULL,
    started_at      REAL NOT NULL,
    expires_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_journal (
    id       TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    kind     TEXT NOT NULL,
    status   TEXT NOT NULL,
    error    TEXT,
    at       REAL NOT NULL
);
"""


@dataclass(frozen=True)
class JournalRow:
    """Итог отправки, который Telegram уже принял, а панель ещё нет. Ни текста, ни адреса, ни chat id здесь нет."""

    id: str
    lease_id: str
    kind: str
    status: str
    error: str | None


@dataclass(frozen=True)
class Watch:
    """Регистрация, за которой бот следит, чтобы сказать человеку итог."""

    registration_id: str
    chat_id: int
    telegram_id: int
    started_at: float


def _key(key: StorageKey) -> str:
    return (
        ":".join(
            str(part)
            for part in (key.bot_id, key.chat_id, key.user_id, key.thread_id or "", key.business_connection_id or "")
        )
        + f":{key.destiny}"
    )


class SqliteStorage(BaseStorage):
    def __init__(self, path: Path, *, ttl: float = DAY, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._ttl = ttl
        self._clock = clock
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()  # одно соединение на все потоки to_thread

    # --- файл --------------------------------------------------------------------------------------------------------

    def _purge(self, connection: sqlite3.Connection) -> None:
        """Просроченное уходит с диска, а не только прячется от чтения: имя и почта не живут дольше ttl."""
        now = self._clock()
        connection.execute("DELETE FROM fsm WHERE expires_at <= ?", (now,))
        connection.execute("DELETE FROM watches WHERE expires_at <= ?", (now,))
        connection.execute("DELETE FROM outbox_journal WHERE at <= ?", (now - DAY,))

    async def purge(self) -> None:
        await self._run(self._purge)

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Права задаём до того, как SQLite создаст файл: журнал наследует права основного файла.
            os.close(os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600))
            connection = sqlite3.connect(self._path, check_same_thread=False)
            connection.execute("PRAGMA secure_delete = ON")
            connection.executescript(_SCHEMA)
            self._purge(connection)
            connection.commit()
            self._connection = connection
        return self._connection

    async def _run(self, work: Callable[[sqlite3.Connection], Any]) -> Any:
        def call() -> Any:
            with self._lock:
                connection = self._connect()
                try:
                    result = work(connection)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                return result

        return await asyncio.to_thread(call)

    async def close(self) -> None:
        def close_connection() -> None:
            with self._lock:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None

        await asyncio.to_thread(close_connection)

    # --- FSM ---------------------------------------------------------------------------------------------------------

    def _write(self, connection: sqlite3.Connection, key: StorageKey, column: str, value: str | None) -> None:
        now, name = self._clock(), _key(key)
        self._purge(connection)  # просроченное не оживает и не копится
        connection.execute(
            f"INSERT INTO fsm (key, state, data, expires_at) VALUES (?, ?, ?, ?) "  # noqa: S608 - column из кода
            f"ON CONFLICT(key) DO UPDATE SET {column} = excluded.{column}, expires_at = excluded.expires_at",
            (name, value if column == "state" else None, value if column == "data" else "{}", now + self._ttl),
        )
        # Диалог закончен (нет состояния и данных) - строки на диске не остаётся.
        connection.execute("DELETE FROM fsm WHERE key = ? AND state IS NULL AND data = '{}'", (name,))

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._run(lambda connection: self._write(connection, key, "state", value))

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._run(
            lambda connection: connection.execute(
                "SELECT state FROM fsm WHERE key = ? AND expires_at > ?", (_key(key), self._clock())
            ).fetchone()
        )
        return None if row is None else row[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        value = json.dumps(dict(data), ensure_ascii=False)
        await self._run(lambda connection: self._write(connection, key, "data", value))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._run(
            lambda connection: connection.execute(
                "SELECT data FROM fsm WHERE key = ? AND expires_at > ?", (_key(key), self._clock())
            ).fetchone()
        )
        return {} if row is None else json.loads(row[0])

    # --- ожидающие регистрации ---------------------------------------------------------------------------------------

    async def add_watch(self, registration_id: str, chat_id: int, telegram_id: int, started_at: float) -> None:
        def add(connection: sqlite3.Connection) -> None:
            self._purge(connection)
            connection.execute(
                "INSERT OR IGNORE INTO watches (registration_id, chat_id, telegram_id, started_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (registration_id, chat_id, telegram_id, started_at, self._clock() + self._ttl),
            )

        await self._run(add)

    async def remove_watch(self, registration_id: str) -> None:
        await self._run(
            lambda connection: connection.execute("DELETE FROM watches WHERE registration_id = ?", (registration_id,))
        )

    async def watches(self) -> list[Watch]:
        rows = await self._run(
            lambda connection: connection.execute(
                "SELECT registration_id, chat_id, telegram_id, started_at FROM watches WHERE expires_at > ?",
                (self._clock(),),
            ).fetchall()
        )
        return [Watch(*row) for row in rows]

    # --- журнал отправленного (очередь сообщений) -------------------------------------------------------------------

    async def journal_put(self, row: JournalRow, at: float) -> None:
        """Записать итог после ответа Telegram. Строка с тем же id заменяется: новая выдача - новая аренда."""
        await self._run(
            lambda connection: connection.execute(
                "INSERT OR REPLACE INTO outbox_journal (id, lease_id, kind, status, error, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row.id, row.lease_id, row.kind, row.status, row.error, at),
            )
        )

    async def journal_all(self) -> list[JournalRow]:
        rows = await self._run(
            lambda connection: connection.execute(
                "SELECT id, lease_id, kind, status, error FROM outbox_journal ORDER BY at"
            ).fetchall()
        )
        return [JournalRow(*row) for row in rows]

    async def journal_by_ids(self, ids: list[str]) -> dict[str, JournalRow]:
        if not ids:
            return {}

        def find(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
            marks = ", ".join("?" for _ in ids)
            return connection.execute(
                f"SELECT id, lease_id, kind, status, error FROM outbox_journal WHERE id IN ({marks})",  # noqa: S608
                ids,
            ).fetchall()

        return {row[0]: JournalRow(*row) for row in await self._run(find)}

    async def journal_remove(self, ids: list[str]) -> None:
        if ids:
            await self._run(
                lambda connection: connection.executemany(
                    "DELETE FROM outbox_journal WHERE id = ?", [(i,) for i in ids]
                )
            )
