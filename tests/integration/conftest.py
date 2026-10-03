"""Интеграция: настоящее HTTP-приложение (ASGI в памяти) с настоящей авторизацией, воркер с Redis-очередью.

Подменяется только SSH (фейковый сервер). Логин — реальный: тестовый админ создаётся в тестовой БД.
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import hash_password
from infrastructure.workers.worker import JobWorker
from main import app
from modules.auth.models.admin import Admin
from modules.jobs.queue import JobQueue
from tests.fakes.fake_ssh import FakeServer


@pytest.fixture(autouse=True)
async def _clean(clean_db: None) -> AsyncIterator[None]:
    yield


@pytest.fixture(autouse=True)
def _no_real_ssh(fakes: dict[UUID, FakeServer]) -> dict[UUID, FakeServer]:
    """Всегда фейковый SSH: интеграционные тесты никогда не ходят в сеть, даже если тест забыл попросить фейк."""
    return fakes


@dataclass
class AdminLogin:
    admin: Admin
    email: str
    password: str


@pytest.fixture
async def admin(session: AsyncSession) -> AdminLogin:
    email, password = f"admin-{secrets.token_hex(4)}@example.com", secrets.token_urlsafe(16)
    admin = Admin(email=email, password_hash=hash_password(password))
    session.add(admin)
    await session.commit()
    return AdminLogin(admin, email, password)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Клиент без авторизации. testserver — имя хоста, под которым httpx хранит cookie."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        yield http


@pytest.fixture
async def api(client: httpx.AsyncClient, admin: AdminLogin) -> httpx.AsyncClient:
    """Тот же клиент после настоящего входа: Bearer-токен в заголовке, refresh — в cookie."""
    response = await client.post("/api/v1/auth/login", json={"email": admin.email, "password": admin.password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    return client


@pytest.fixture
def worker() -> JobWorker:
    return JobWorker(JobQueue())


async def drain(worker: JobWorker, limit: int = 50) -> int:
    """Выполняет все задачи из очереди (как работающий воркер) и возвращает, сколько их было."""
    done = 0
    while await worker.process_next(timeout=1):
        done += 1
        assert done < limit, "queue does not drain — a job keeps re-queueing itself"
    return done
