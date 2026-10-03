"""Общая настройка тестов. Выполняется pytest ДО импорта кода приложения.

Окружение задаётся здесь явно (переменные окружения важнее .env), поэтому тесты:
- никогда не трогают рабочую БД vpn_panel и Redis db 0;
- не зависят от секретов из .env (свои тестовые ключи JWT и шифрования).
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://user:password@localhost:5432/vpn_panel_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

os.environ.update(
    {
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "JWT_SECRET_KEY": "test-only-jwt-secret-0123456789abcdef",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "JWT_ACCESS_EXPIRE": "900",
        "JWT_REFRESH_EXPIRE": "86400",
        "SSH_CONNECT_TIMEOUT": "5",
        "SSH_COMMAND_TIMEOUT": "5",
        "SCHEDULER_ENABLED": "false",
        "COOKIE_SECURE": "false",  # httpx в тестах ходит по http://testserver — Secure-cookie он бы не вернул
    }
)

from infrastructure.vpn import amnezia  # noqa: E402 — только после настройки окружения

# Общие фикстуры с БД (session, fakes, world) — для tests/services и tests/integration.
pytest_plugins = ["tests.fixtures"]


@pytest.fixture(autouse=True)
def no_container_wait(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Провайдер ждёт 5 с после docker run, чтобы поймать «упал сразу после старта». С фейком ждать нечего.

    Подменяем asyncio только внутри модуля amnezia, а не глобально: глобальная подмена сломала бы pytest-asyncio.
    """

    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr(amnezia, "asyncio", SimpleNamespace(sleep=instant))
    yield


def run_alembic(*args: str) -> None:
    """Alembic в отдельном процессе: его env.py сам делает asyncio.run, а внутри нашего цикла так нельзя."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture(scope="session")
def database() -> Iterator[None]:
    """Чистая тестовая БД на прогон: пересоздаётся и доводится миграциями до head (заодно проверяем миграции)."""
    url = make_url(TEST_DATABASE_URL)
    # Предохранитель: дальше идут DROP DATABASE и TRUNCATE — только для БД с суффиксом _test.
    assert url.database and url.database.endswith("_test"), f"refusing to use non-test database {url.database!r}"
    assert not TEST_REDIS_URL.rstrip("/").endswith("/0"), "refusing to flush Redis db 0"

    async def recreate() -> None:
        conn = await asyncpg.connect(
            user=url.username, password=url.password, host=url.host, port=url.port, database="postgres"
        )
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{url.database}"')
        finally:
            await conn.close()

    asyncio.run(recreate())
    run_alembic("upgrade", "head")
    yield


@pytest.fixture
async def clean_db(database: None) -> AsyncIterator[None]:
    """После каждого теста — пустые таблицы и пустой тестовый Redis: тесты не влияют друг на друга."""
    from sqlalchemy import text

    import main  # noqa: F401 — регистрирует все ORM-модели в Base.metadata
    from core.database.base import Base
    from core.database.postgres import engine
    from core.redis import get_redis

    yield
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await get_redis().flushdb()
