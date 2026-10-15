"""Консольное создание админа (этап 20.4): меню, случайный пароль (виден один раз и только в терминале), временный
пароль закрывает панель до смены, идемпотентность для установщика."""

import re
from collections.abc import Iterable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import verify_password
from modules.audit.models.audit_log import AuditAction, AuditLog
from modules.auth.models.admin import Admin, AdminRole
from scripts.create_admin import CliError, Console, generate_password, run

EMAIL = "first@example.com"
CUSTOM = "my-own-long-password"
GENERATED = re.compile(r"\b[a-km-zA-HJ-NP-Z2-9]{5}(?:-[a-km-zA-HJ-NP-Z2-9]{5}){3}\b")


class Terminal:
    """Подставной терминал: ответы на вопросы заданы заранее, всё сказанное запоминается."""

    def __init__(self, answers: Iterable[str] = (), secrets: Iterable[str] = (), interactive: bool = True):
        self.said: list[str] = []
        self._answers = iter(answers)
        self._secrets = iter(secrets)
        self.console = Console(
            interactive=interactive,
            ask=lambda prompt: self._next(self._answers, prompt),
            ask_secret=lambda prompt: self._next(self._secrets, prompt),
            say=self.said.append,
        )

    def _next(self, source: Iterable[str], prompt: str) -> str:
        self.said.append(prompt)  # вопрос тоже виден на экране
        try:
            return next(iter(source))
        except StopIteration:
            raise AssertionError(f"unexpected question: {prompt!r}") from None

    @property
    def screen(self) -> str:
        return "\n".join(self.said)


async def admins(session: AsyncSession) -> list[Admin]:
    session.expire_all()
    return list((await session.execute(select(Admin).order_by(Admin.created_at))).scalars())


def shown_password(terminal: Terminal) -> str:
    found = GENERATED.findall(terminal.screen)
    assert len(found) == 1, f"the password must be shown exactly once, saw {len(found)}"
    return found[0]


# --- случайный пароль -----------------------------------------------------------------------------------------------


async def test_random_password_is_shown_once_and_is_temporary(session: AsyncSession) -> None:
    terminal = Terminal()
    await run([EMAIL, "--random-password"], terminal.console)

    (admin,) = await admins(session)
    password = shown_password(terminal)
    assert admin.email == EMAIL and admin.role == AdminRole.SUPERADMIN
    assert admin.must_change_password is True  # пароль знает тот, кто смотрел на экран, — временный
    assert verify_password(password, admin.password_hash) and password not in admin.password_hash
    assert "задать свой" in terminal.screen  # сказано, что панель попросит сменить


async def test_generated_password_cannot_land_in_a_log(session: AsyncSession) -> None:
    """Вывод не в терминал (файл, `| tee install.log`, CI) — отказ ДО создания: пароль в логе не окажется."""
    terminal = Terminal(interactive=False)
    with pytest.raises(CliError, match="терминал"):
        await run([EMAIL, "--random-password"], terminal.console)

    assert await admins(session) == []
    assert not GENERATED.search(terminal.screen)


async def test_generated_passwords_are_strong_unique_and_unambiguous() -> None:
    samples = {generate_password() for _ in range(2000)}
    assert len(samples) == 2000
    assert all(GENERATED.fullmatch(p) and len(p) == 23 for p in samples)
    assert not any(set(p) & set("lIO01") for p in samples)  # такой пароль переписывают с экрана руками


# --- меню -----------------------------------------------------------------------------------------------------------


async def test_menu_default_is_a_generated_password(session: AsyncSession) -> None:
    terminal = Terminal(answers=[EMAIL, ""])  # email, затем Enter в меню
    await run([], terminal.console)

    (admin,) = await admins(session)
    assert admin.must_change_password is True
    assert verify_password(shown_password(terminal), admin.password_hash)


async def test_menu_own_password_is_permanent_and_never_printed(session: AsyncSession) -> None:
    terminal = Terminal(answers=[EMAIL, "2"], secrets=[CUSTOM, CUSTOM])
    await run([], terminal.console)

    (admin,) = await admins(session)
    assert admin.must_change_password is False  # пароль выбрал сам админ
    assert verify_password(CUSTOM, admin.password_hash)
    assert CUSTOM not in terminal.screen


@pytest.mark.parametrize(
    ("secrets", "message"),
    [(["short", "short"], "не короче 12"), ([CUSTOM, CUSTOM + "x"], "не совпадают")],
)
async def test_menu_own_password_is_validated(session: AsyncSession, secrets: list[str], message: str) -> None:
    terminal = Terminal(answers=[EMAIL, "2"], secrets=secrets)
    with pytest.raises(CliError, match=message):
        await run([], terminal.console)
    assert await admins(session) == []


async def test_menu_asks_again_on_garbage_choice(session: AsyncSession) -> None:
    terminal = Terminal(answers=[EMAIL, "7", "1"])
    await run([], terminal.console)
    assert len(await admins(session)) == 1


# --- email, роль, дубликаты -----------------------------------------------------------------------------------------


async def test_email_is_required_without_a_terminal() -> None:
    with pytest.raises(CliError, match="email"):
        await run(["--random-password"], Terminal(interactive=False).console)


@pytest.mark.parametrize("email", ["not-an-email", "a@", "@b.com"])
async def test_invalid_email_is_rejected(session: AsyncSession, email: str) -> None:
    with pytest.raises(CliError, match="email"):
        await run([email, "--random-password"], Terminal().console)
    assert await admins(session) == []


async def test_existing_email_is_not_overwritten(session: AsyncSession) -> None:
    await run([EMAIL, "--random-password"], Terminal().console)
    (before,) = await admins(session)
    with pytest.raises(CliError, match="уже"):
        await run([EMAIL, "--random-password"], Terminal().console)
    (after,) = await admins(session)
    assert after.password_hash == before.password_hash  # чужой пароль не затёрт


@pytest.mark.parametrize("role", list(AdminRole))
async def test_role_option(session: AsyncSession, role: AdminRole) -> None:
    await run([EMAIL, "--random-password", "--role", role.value], Terminal().console)
    (admin,) = await admins(session)
    assert admin.role == role


# --- повторный запуск установщика -----------------------------------------------------------------------------------


async def test_only_if_empty_creates_the_first_admin(session: AsyncSession) -> None:
    await run([EMAIL, "--random-password", "--only-if-empty"], Terminal().console)
    assert len(await admins(session)) == 1


async def test_only_if_empty_does_nothing_when_an_admin_exists(session: AsyncSession) -> None:
    await run([EMAIL, "--random-password"], Terminal().console)
    rerun = Terminal()
    await run(["second@example.com", "--random-password", "--only-if-empty"], rerun.console)

    assert len(await admins(session)) == 1
    assert not GENERATED.search(rerun.screen)  # повторный запуск не печатает паролей и ничего не создаёт


# --- аудит и вход ---------------------------------------------------------------------------------------------------


async def test_creation_is_audited_without_the_password(session: AsyncSession) -> None:
    terminal = Terminal()
    await run([EMAIL, "--random-password"], terminal.console)

    (entry,) = (await session.execute(select(AuditLog).where(AuditLog.action == AuditAction.ADMIN_CREATED))).scalars()
    assert entry.admin_id is None and entry.entity_id is not None  # создал не админ панели, а консоль
    assert entry.meta == {"email": EMAIL, "role": "superadmin", "must_change_password": True, "source": "cli"}
    assert shown_password(terminal) not in str(entry.meta)


async def test_generated_admin_signs_in_but_is_locked_until_password_change(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    terminal = Terminal()
    await run([EMAIL, "--random-password"], terminal.console)
    login = await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": shown_password(terminal)})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (await client.get("/api/v1/servers", headers=headers)).status_code == 403  # панель закрыта
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["must_change_password"] is True
