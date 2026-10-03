"""Rate limiting: счётчик на Redis, вход по IP и по учётке, refresh по IP, 429 + Retry-After, fail-open."""

import asyncio

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.rate_limit import Limit, RateLimiter, RateLimitExceeded, subject_key
from core.security import DUMMY_PASSWORD_HASH
from main import app
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.auth import service as auth_service
from modules.auth.service import AuthService
from tests.integration.conftest import AdminLogin

LIMIT = Limit("test", max_hits=3, window_seconds=60)


async def login(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def audit_actions(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    return list((await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


# --- сам ограничитель ------------------------------------------------------------------------------------------------


async def test_fixed_window_counts_per_subject() -> None:
    limiter = RateLimiter()
    for _ in range(3):
        await limiter.hit(LIMIT, "a")
    with pytest.raises(RateLimitExceeded) as exc:
        await limiter.hit(LIMIT, "a")
    assert 0 < exc.value.retry_after <= 60
    await limiter.hit(LIMIT, "b")  # у другого субъекта свой счётчик


async def test_failures_counter_checks_without_counting() -> None:
    limiter = RateLimiter()
    for _ in range(5):
        await limiter.ensure_not_exceeded(LIMIT, "x")  # проверка сама попытку не засчитывает
    for _ in range(3):
        await limiter.record(LIMIT, "x")
    with pytest.raises(RateLimitExceeded):
        await limiter.ensure_not_exceeded(LIMIT, "x")
    await limiter.reset(LIMIT, "x")
    await limiter.ensure_not_exceeded(LIMIT, "x")


async def test_redis_down_fails_open() -> None:
    broken = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    limiter = RateLimiter(broken)
    for _ in range(5):
        await limiter.hit(LIMIT, "a")  # без Redis лимит не проверяется, запрос проходит
    await limiter.ensure_not_exceeded(LIMIT, "a")
    assert await limiter.record(LIMIT, "a") == 0
    await limiter.reset(LIMIT, "a")
    await broken.aclose()


def test_subject_key_hides_email() -> None:
    key = subject_key(" Admin@Example.com ")
    assert key == subject_key("admin@example.com") and "example" not in key


# --- вход ------------------------------------------------------------------------------------------------------------


async def test_login_limited_by_ip(client: httpx.AsyncClient) -> None:
    for i in range(settings.login_ip_max_attempts):
        assert (await login(client, f"nobody{i}@example.com", "wrong-password")).status_code == 401
    response = await login(client, "another@example.com", "wrong-password")
    assert response.status_code == 429 and response.json() == {"detail": "Too many requests"}
    assert 0 < int(response.headers["retry-after"]) <= settings.login_ip_window_seconds


async def test_account_locked_after_failures_even_with_right_password(
    client: httpx.AsyncClient, admin: AdminLogin, session: AsyncSession
) -> None:
    for _ in range(settings.login_max_failures):
        assert (await login(client, admin.email, "wrong-password")).status_code == 401
    locked = await login(client, admin.email, admin.password)
    assert locked.status_code == 429  # верный пароль во время блокировки не пускает и не выдаёт себя
    assert int(locked.headers["retry-after"]) <= settings.login_lock_seconds
    assert len(await audit_actions(session, AuditAction.LOGIN_FAILED)) == settings.login_max_failures
    (lock,) = await audit_actions(session, AuditAction.LOGIN_LOCKED)  # одна запись на блокировку
    assert lock.meta == {"email": admin.email, "scope": "ip", "lock_seconds": settings.login_lock_seconds}


async def test_unknown_email_locks_the_same_way(client: httpx.AsyncClient) -> None:
    """Иначе по «блокируется / не блокируется» можно было бы узнать, существует ли учётка."""
    for _ in range(settings.login_max_failures):
        assert (await login(client, "ghost@example.com", "wrong-password")).status_code == 401
    assert (await login(client, "ghost@example.com", "wrong-password")).status_code == 429


async def test_successful_login_resets_failures(client: httpx.AsyncClient, admin: AdminLogin) -> None:
    for _ in range(settings.login_max_failures - 1):
        await login(client, admin.email, "wrong-password")
    assert (await login(client, admin.email, admin.password)).status_code == 200
    for _ in range(settings.login_max_failures - 1):
        assert (await login(client, admin.email, "wrong-password")).status_code == 401
    assert (await login(client, admin.email, admin.password)).status_code == 200  # счёт начался заново


async def test_unknown_email_still_runs_argon2(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[str] = []

    def spy(password: str, hashed: str) -> bool:
        checked.append(hashed)
        return False

    monkeypatch.setattr(auth_service, "verify_password", spy)
    with pytest.raises(auth_service.InvalidCredentialsError):
        await AuthService(session, SYSTEM_ACTOR).authenticate("ghost@example.com", "whatever")
    assert checked == [DUMMY_PASSWORD_HASH]  # время ответа как при неверном пароле


# --- refresh ---------------------------------------------------------------------------------------------------------


async def test_refresh_limited_by_ip(client: httpx.AsyncClient) -> None:
    for _ in range(settings.refresh_ip_max_attempts):
        assert (await client.post("/api/v1/auth/refresh")).status_code == 401
    assert (await client.post("/api/v1/auth/refresh")).status_code == 429


async def test_argon2_runs_off_the_event_loop(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Argon2 (~100 мс CPU) в event loop остановил бы все запросы API на время каждой проверки пароля."""
    threads: list[bool] = []

    def spy(password: str, hashed: str) -> bool:
        try:
            asyncio.get_running_loop()
            threads.append(False)  # вызвали прямо в event loop
        except RuntimeError:
            threads.append(True)  # отдельный поток: цикл свободен
        return False

    monkeypatch.setattr(auth_service, "verify_password", spy)
    with pytest.raises(auth_service.InvalidCredentialsError):
        await AuthService(session, SYSTEM_ACTOR).authenticate("ghost@example.com", "whatever")
    assert threads == [True]


def client_from(ip: str) -> httpx.AsyncClient:
    """Клиент с заданным IP — как разные адреса за nginx (в аудите и лимитах это request.client)."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 40000)), base_url="http://testserver")


async def test_attacker_cannot_lock_admin_out_from_another_ip(admin: AdminLogin) -> None:
    """Раньше 5 неудач с любого адреса закрывали учётку для всех: зная email, можно было держать админа снаружи."""
    async with client_from("198.51.100.66") as attacker, client_from("203.0.113.7") as office:
        for _ in range(settings.login_max_failures):
            assert (await login(attacker, admin.email, "wrong-password")).status_code == 401
        assert (await login(attacker, admin.email, admin.password)).status_code == 429  # себя атакующий запер
        assert (await login(office, admin.email, admin.password)).status_code == 200  # админа — нет


async def test_account_threshold_still_stops_botnet(
    admin: AdminLogin, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Перебор с многих адресов (каждый ниже порога «учётка+IP») упирается в общий порог учётки."""
    monkeypatch.setattr(auth_service, "LOGIN_FAILURES_BY_EMAIL", Limit("login_email_failures", 3, 900))
    for n in range(3):
        async with client_from(f"198.51.100.{n + 1}") as bot:
            assert (await login(bot, admin.email, "wrong-password")).status_code == 401
    async with client_from("203.0.113.7") as office:
        assert (await login(office, admin.email, admin.password)).status_code == 429  # закрыто целиком
    scopes = [log.meta["scope"] for log in await audit_actions(session, AuditAction.LOGIN_LOCKED)]
    assert scopes == ["account"]
