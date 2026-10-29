"""Общая настройка тестов. Выполняется pytest ДО импорта кода приложения.

Окружение задаётся здесь явно (переменные окружения важнее .env), поэтому тесты:
- никогда не трогают рабочую БД vpn_panel и Redis db 0;
- не зависят от секретов из .env (свои тестовые ключи JWT и шифрования).
"""

import asyncio
import os
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

# pytest-xdist (`-n 4`): у каждого процесса своя БД и свой номер Redis — иначе DROP DATABASE и FLUSHDB одного
# процесса ломают тесты соседа. gw0 → vpn_panel_gw0_test + Redis db 14, gw1 → db 13 и т.д. (db 0 никогда).
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
if _XDIST_WORKER:
    _n = int(_XDIST_WORKER.removeprefix("gw"))
    assert _n < 14, "не больше 14 процессов xdist: у Redis 16 баз, 0 и 15 заняты"
    _db_url = make_url(TEST_DATABASE_URL)
    TEST_DATABASE_URL = _db_url.set(
        database=_db_url.database.removesuffix("_test") + f"_{_XDIST_WORKER}_test"  # type: ignore[union-attr]
    ).render_as_string(hide_password=False)
    TEST_REDIS_URL = TEST_REDIS_URL.rsplit("/", 1)[0] + f"/{14 - _n}"

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
        "PUBLIC_URL": "https://panel.example.test",  # адрес в ссылках на страницу конфига (этап 25.3)
        "COOKIE_SECURE": "false",  # httpx в тестах ходит по http://testserver — Secure-cookie он бы не вернул
    }
)

from argon2 import PasswordHasher  # noqa: E402

from core import security  # noqa: E402 — только после настройки окружения
from infrastructure.vpn import amnezia  # noqa: E402 — только после настройки окружения

# Argon2 с параметрами по умолчанию — ~0.25 с на hash+verify, а он есть почти в каждом интеграционном тесте
# (создание админа + вход). Параметры записаны в самом хеше, так что проверка работает и для дешёвых хешей.
security.ph = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
security.DUMMY_PASSWORD_HASH = security.ph.hash("dummy-password-for-timing")

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


@pytest.fixture(autouse=True)
def no_geoip_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Задача GEOIP_UPDATE (её ставит установка шлюза без списка и планировщик) не должна ходить в сеть: по умолчанию
    скачивание падает; тесты geoip подменяют его сами."""
    from infrastructure.geoip import DownloadError
    from modules.geoip import service as geoip_service

    async def refuse(url: str, max_bytes: int, timeout: int = 120) -> str:
        raise DownloadError("The network is disabled in tests")

    monkeypatch.setattr(geoip_service, "download_text", refuse)


def run_alembic(*args: str) -> None:
    """Alembic в том же процессе, в отдельном потоке: env.py сам делает asyncio.run, а в нашем цикле так нельзя.

    Подпроцесс стоил ~4 с на каждый вызов (запуск Python и импорт всех моделей) и в сумме давал большую часть времени
    тестов миграций. Config без alembic.ini: так env.py не вызывает fileConfig и не ломает логирование остальных тестов.
    """
    from concurrent.futures import ThreadPoolExecutor

    from alembic.config import Config

    from alembic import command

    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    action, rest = getattr(command, args[0]), args[1:]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(action, config, *rest).result()
    except Exception as error:
        raise RuntimeError(f"alembic {' '.join(args)} failed: {error!r}") from error


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


# Медленные по природе: каждый тест запускает bash-скрипт (install/panel/certbot) или alembic в подпроцессе.
# Нужны, когда менялись deploy/*.sh, install.sh, panel, alembic/ — иначе локально гоняем `-m "not slow"`.
_SLOW_FILES = ("test_install_", "test_panel", "test_letsencrypt", "test_migrations")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if Path(str(item.fspath)).name.startswith(_SLOW_FILES):
            item.add_marker(pytest.mark.slow)
