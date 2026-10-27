"""PanelClient: единственный путь бота к панели. Ретраи, коды ответов, адрес, TLS, отсутствие секретов в ошибках.

Панель - `FakePanel` (httpx.MockTransport); паузы клиента записываются, а не ждутся. Настоящий TLS-сервер - в конце."""

import asyncio
import logging
import os
import shutil
import ssl
import subprocess
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fakes import (
    API_PREFIX,
    CONFIG_LINK,
    FakeClock,
    FakePanel,
    Outcome,
    conflict,
    dropped,
    rate_limited,
    redirect,
    rejected,
    timed_out,
    unauthorized,
    unavailable,
)
from support import CODE, EMAIL, NAME, PANEL_URL, SECRET_KEY, TG_ID
from tollwire_bot import __version__
from tollwire_bot.panel import (
    Conflict,
    PanelAuthError,
    PanelClient,
    PanelUnavailable,
    RateLimited,
    Rejected,
)

FAILURES: list[tuple[str, Callable[[], Outcome]]] = [
    ("502", lambda: unavailable(502)),
    ("503", lambda: unavailable(503)),
    ("504", lambda: unavailable(504)),
    ("connection dropped", dropped),
    ("timeout", timed_out),
]


@dataclass
class Naps:
    """Вместо asyncio.sleep: записывает, сколько клиент собирался ждать."""

    delays: list[float] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel(FakeClock())


@pytest.fixture
def naps() -> Naps:
    return Naps()


@pytest.fixture
async def client(panel: FakePanel, naps: Naps) -> AsyncIterator[PanelClient]:
    panel_client = PanelClient(PANEL_URL, panel.key, transport=panel.transport, sleep=naps)
    yield panel_client
    await panel_client.aclose()


async def register(client: PanelClient, **fields: Any) -> Any:
    values: dict[str, Any] = {
        "code": CODE,
        "telegram_id": TG_ID,
        "telegram_username": "ivan_p",
        "name": NAME,
        "email": EMAIL,
    }
    return await client.register(**{**values, **fields})


# --- методы и запросы ------------------------------------------------------------------------------------------------


async def test_every_request_carries_the_bot_key_and_user_agent(panel: FakePanel, client: PanelClient) -> None:
    await client.heartbeat("tollwire_test_bot", __version__)

    (call,) = panel.calls
    assert call.headers["x-bot-key"] == panel.key
    assert call.headers["user-agent"] == f"tollwire-bot/{__version__}"


async def test_heartbeat_sends_name_and_version(panel: FakePanel, client: PanelClient) -> None:
    await client.heartbeat("tollwire_test_bot", "1.2.3")

    assert panel.bodies("POST /heartbeat") == [{"bot_username": "tollwire_test_bot", "version": "1.2.3"}]


@pytest.mark.parametrize(("status", "code"), [("valid", "A"), ("used", "B"), ("expired", "C"), ("revoked", "D")])
async def test_invite_check_returns_the_status_of_the_code(
    panel: FakePanel, client: PanelClient, status: str, code: str
) -> None:
    panel.add_invite(code, status)

    assert await client.check_invite(code) == status
    assert panel.bodies("POST /invites/check") == [{"code": code}]


async def test_unknown_invite_is_a_status_not_an_error(client: PanelClient) -> None:
    assert await client.check_invite("no-such-code") == "unknown"


async def test_register_sends_the_person_and_returns_the_registration(panel: FakePanel, client: PanelClient) -> None:
    panel.add_invite(CODE)

    registration = await register(client)

    (body,) = panel.bodies("POST /registrations")
    assert body == {"code": CODE, "telegram_id": TG_ID, "telegram_username": "ivan_p", "name": NAME, "email": EMAIL}
    assert registration.status == "provisioning" and getattr(registration, "config_url", None) is None
    assert registration.registration_id in panel.registrations


async def test_register_without_a_telegram_username_sends_none(panel: FakePanel, client: PanelClient) -> None:
    panel.add_invite(CODE)

    await register(client, telegram_username=None)

    (body,) = panel.bodies("POST /registrations")
    assert body.get("telegram_username") is None


async def test_registration_status_is_read_by_id(panel: FakePanel, client: PanelClient) -> None:
    panel.add_invite(CODE)
    created = await register(client)
    panel.finish(created.registration_id, "active")

    registration = await client.registration(created.registration_id)

    assert registration.status == "active" and getattr(registration, "config_url", None) is None
    assert panel.calls[-1].path == f"{API_PREFIX}/registrations/{created.registration_id}"


async def test_unknown_registration_is_none(client: PanelClient) -> None:
    assert await client.registration("00000000-0000-4000-8000-000000000000") is None


async def test_user_by_telegram_returns_the_status_fields(panel: FakePanel, client: PanelClient) -> None:
    panel.add_user(TG_ID, NAME)

    user = await client.user_by_telegram(TG_ID)

    assert user is not None and user.name == NAME and user.protocol == "amneziawg" and user.valid_until
    assert panel.calls[-1].path == f"{API_PREFIX}/users/by-telegram/{TG_ID}"


async def test_unknown_user_is_none(client: PanelClient) -> None:
    assert await client.user_by_telegram(TG_ID) is None


async def test_config_link_posts_to_the_person_and_returns_url_and_date(panel: FakePanel, client: PanelClient) -> None:
    panel.add_user(TG_ID, NAME)

    link = await client.config_link(TG_ID)

    assert link is not None and link.config_url == panel.links[0].url and link.valid_until
    (call,) = [call for call in panel.calls if call.route == CONFIG_LINK]
    assert call.method == "POST" and call.path == f"{API_PREFIX}/users/by-telegram/{TG_ID}/config-link"
    assert call.headers["x-bot-key"] == panel.key


async def test_config_link_of_an_unknown_person_is_none(client: PanelClient) -> None:
    assert await client.config_link(TG_ID) is None


@pytest.mark.parametrize("reason", ["config_unavailable", "not_ready", "no_access", "not_configured"])
async def test_config_link_refusals_keep_their_reason(panel: FakePanel, client: PanelClient, reason: str) -> None:
    panel.add_user(TG_ID, NAME)
    panel.link_refusal[TG_ID] = reason

    with pytest.raises(Conflict) as caught:
        await client.config_link(TG_ID)

    assert caught.value.reason == reason
    assert panel.links == []


async def test_config_link_with_an_unknown_reason_is_a_plain_conflict(panel: FakePanel, client: PanelClient) -> None:
    panel.add_user(TG_ID, NAME)
    panel.override(CONFIG_LINK, conflict("something_new"))

    with pytest.raises(Conflict) as caught:
        await client.config_link(TG_ID)

    assert caught.value.reason == "conflict"


async def test_config_link_is_retried_like_any_other_request_on_a_panel_outage(
    panel: FakePanel, client: PanelClient, naps: Naps
) -> None:
    panel.add_user(TG_ID, NAME)
    panel.override(CONFIG_LINK, unavailable(), times=2)

    link = await client.config_link(TG_ID)

    assert link is not None and naps.delays == [0.5, 1]


async def test_config_link_gives_up_after_the_retries_with_panel_unavailable(
    panel: FakePanel, client: PanelClient
) -> None:
    panel.add_user(TG_ID, NAME)
    panel.override(CONFIG_LINK, unavailable(), times=5)

    with pytest.raises(PanelUnavailable):
        await client.config_link(TG_ID)


async def test_config_link_waits_out_a_short_rate_limit_but_not_a_long_one(
    panel: FakePanel, client: PanelClient, naps: Naps
) -> None:
    panel.add_user(TG_ID, NAME)
    panel.override(CONFIG_LINK, rate_limited(3))
    assert await client.config_link(TG_ID) is not None and naps.delays == [3]

    panel.override(CONFIG_LINK, rate_limited(60))
    with pytest.raises(RateLimited):
        await client.config_link(TG_ID)


async def test_config_link_rejects_a_wrong_key_without_retries(
    panel: FakePanel, client: PanelClient, naps: Naps
) -> None:
    panel.add_user(TG_ID, NAME)
    panel.override(CONFIG_LINK, unauthorized())

    with pytest.raises(PanelAuthError):
        await client.config_link(TG_ID)

    assert naps.delays == []


async def test_an_answer_without_the_link_is_panel_unexpected(panel: FakePanel, client: PanelClient) -> None:
    import httpx

    from tollwire_bot.panel import PanelUnexpected

    panel.add_user(TG_ID, NAME)
    panel.override(CONFIG_LINK, lambda: httpx.Response(200, json={"valid_until": "2026-11-25T12:00:00Z"}))

    with pytest.raises(PanelUnexpected):
        await client.config_link(TG_ID)


async def test_the_url_of_a_link_never_reaches_the_log(
    panel: FakePanel, client: PanelClient, caplog: pytest.LogCaptureFixture
) -> None:
    panel.add_user(TG_ID, NAME)

    with caplog.at_level(logging.DEBUG):
        link = await client.config_link(TG_ID)
        panel.override(CONFIG_LINK, unavailable(), times=5)
        with pytest.raises(PanelUnavailable):
            await client.config_link(TG_ID)

    assert link is not None
    logs = "\n".join(record.getMessage() + str(vars(record)) for record in caplog.records)
    assert panel.links[0].token not in logs and "/c#" not in logs


async def test_only_the_bot_api_is_ever_called(panel: FakePanel, client: PanelClient) -> None:
    panel.add_invite(CODE)
    await client.heartbeat("tollwire_test_bot", __version__)
    await client.check_invite(CODE)
    created = await register(client)
    await client.registration(created.registration_id)
    await client.user_by_telegram(TG_ID)
    panel.finish(created.registration_id)
    await client.config_link(TG_ID)

    assert len(panel.calls) == 6
    assert all(call.path.startswith(API_PREFIX + "/") for call in panel.calls)


async def test_trailing_slash_in_the_panel_url_does_not_double_the_slash(panel: FakePanel, naps: Naps) -> None:
    client = PanelClient(PANEL_URL + "/", panel.key, transport=panel.transport, sleep=naps)
    try:
        await client.heartbeat("tollwire_test_bot", __version__)
    finally:
        await client.aclose()

    assert panel.calls[0].path == f"{API_PREFIX}/heartbeat"


# --- адрес -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://panel.example.com", "https://panel.example.com:8443", "http://api:8000"])
async def test_https_and_internal_http_addresses_are_accepted(url: str) -> None:
    await PanelClient(url, SECRET_KEY).aclose()


@pytest.mark.parametrize(
    "url",
    ["http://panel.example.com", "http://10.0.0.5:8000", "http://203.0.113.7", "ftp://panel.example.com", "panel", ""],
)
def test_plain_http_to_a_real_host_is_refused(url: str) -> None:
    """Ключ бота не должен идти открытым текстом по интернету; http - только для имён сети compose (без точки)."""
    with pytest.raises(ValueError):
        PanelClient(url, SECRET_KEY)


# --- ретраи ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "failure"), FAILURES, ids=[label for label, _ in FAILURES])
async def test_a_short_outage_is_retried_with_growing_pauses(
    panel: FakePanel, client: PanelClient, naps: Naps, label: str, failure: Callable[[], Outcome]
) -> None:
    panel.add_invite(CODE)
    panel.override("POST /invites/check", failure(), times=2)

    assert await client.check_invite(CODE) == "valid"

    assert naps.delays == [0.5, 1]
    assert panel.count("POST /invites/check") == 3


@pytest.mark.parametrize(("label", "failure"), FAILURES, ids=[label for label, _ in FAILURES])
async def test_a_long_outage_ends_in_panel_unavailable_after_four_pauses(
    panel: FakePanel, client: PanelClient, naps: Naps, label: str, failure: Callable[[], Outcome]
) -> None:
    panel.override("GET /users/by-telegram/*", failure(), times=10)

    with pytest.raises(PanelUnavailable):
        await client.user_by_telegram(TG_ID)

    assert naps.delays == [0.5, 1, 2, 4]
    assert panel.count("GET /users/by-telegram/*") == 5


async def test_a_retried_registration_repeats_the_same_request(
    panel: FakePanel, client: PanelClient, naps: Naps
) -> None:
    """POST /registrations безопасно повторять: панель вернёт ту же регистрацию тому же человеку."""
    panel.add_invite(CODE)
    panel.override("POST /registrations", dropped(), times=2)

    registration = await register(client)

    first, second, third = panel.bodies("POST /registrations")
    assert first == second == third and registration.status == "provisioning"
    assert len(panel.registrations) == 1


async def test_a_client_error_is_not_retried(panel: FakePanel, client: PanelClient, naps: Naps) -> None:
    panel.add_invite(CODE, "used")

    with pytest.raises(Conflict):
        await register(client)

    assert panel.count("POST /registrations") == 1 and naps.delays == []


async def test_unavailable_error_carries_no_secrets(panel: FakePanel, client: PanelClient) -> None:
    panel.override("POST /registrations", unavailable(), times=10)

    with pytest.raises(PanelUnavailable) as caught:
        await register(client)

    shown = f"{caught.value!s} {caught.value!r}"
    assert panel.key not in shown and EMAIL not in shown and NAME not in shown and CODE not in shown


# --- коды ответов ----------------------------------------------------------------------------------------------------


async def test_a_rejected_key_is_an_auth_error_without_retries(
    panel: FakePanel, naps: Naps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = PanelClient(PANEL_URL, "twb_" + "x" * 43, transport=panel.transport, sleep=naps)
    try:
        with pytest.raises(PanelAuthError):
            await client.heartbeat("tollwire_test_bot", __version__)
    finally:
        await client.aclose()

    assert panel.count("POST /heartbeat") == 1 and naps.delays == []
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1  # человеку, читающему лог, нужна одна строка «ключ неверный или отозван»
    assert "twb_" + "x" * 43 not in caplog.text and panel.key not in caplog.text


async def test_a_revoked_key_is_the_same_auth_error(panel: FakePanel, client: PanelClient) -> None:
    panel.override("GET /users/by-telegram/*", unauthorized())

    with pytest.raises(PanelAuthError):
        await client.user_by_telegram(TG_ID)


@pytest.mark.parametrize(
    "reason", ["invite_invalid", "invite_expired", "invite_used", "telegram_taken", "email_taken", "conflict"]
)
async def test_conflict_carries_the_reason(panel: FakePanel, client: PanelClient, reason: str) -> None:
    panel.override("POST /registrations", conflict(reason))

    with pytest.raises(Conflict) as caught:
        await register(client)

    assert caught.value.reason == reason


@pytest.mark.parametrize("reason", ["something_new", "", None])
async def test_an_unknown_or_missing_reason_is_plain_conflict(
    panel: FakePanel, client: PanelClient, reason: str | None
) -> None:
    panel.override("POST /registrations", conflict(reason))

    with pytest.raises(Conflict) as caught:
        await register(client)

    assert caught.value.reason == "conflict"


@pytest.mark.parametrize("field_name", ["name", "email", "telegram_id"])
async def test_validation_error_names_the_field(panel: FakePanel, client: PanelClient, field_name: str) -> None:
    panel.override("POST /registrations", rejected(field_name))

    with pytest.raises(Rejected) as caught:
        await register(client)

    assert caught.value.field == field_name
    assert panel.count("POST /registrations") == 1


async def test_validation_detail_with_the_input_is_never_kept_or_logged(
    panel: FakePanel, client: PanelClient, caplog: pytest.LogCaptureFixture
) -> None:
    """В detail 422 - введённое значение (почта, имя): ни в исключении, ни в логах его быть не должно."""
    caplog.set_level(logging.DEBUG)
    panel.override("POST /registrations", rejected("email", echo=EMAIL))

    with pytest.raises(Rejected) as caught:
        await register(client)

    shown = f"{caught.value!s} {caught.value!r} {caplog.text}"
    assert EMAIL not in shown and "Value error" not in shown


# --- 429 -------------------------------------------------------------------------------------------------------------


async def test_a_short_rate_limit_is_waited_out_once(panel: FakePanel, client: PanelClient, naps: Naps) -> None:
    panel.add_invite(CODE)
    panel.override("POST /invites/check", rate_limited(3))

    assert await client.check_invite(CODE) == "valid"

    assert naps.delays == [3] and panel.count("POST /invites/check") == 2


async def test_a_second_rate_limit_is_given_up_on(panel: FakePanel, client: PanelClient, naps: Naps) -> None:
    panel.override("POST /invites/check", rate_limited(3), times=5)

    with pytest.raises(RateLimited):
        await client.check_invite(CODE)

    assert naps.delays == [3] and panel.count("POST /invites/check") == 2  # повторили один раз, не больше


async def test_a_boundary_retry_after_of_ten_seconds_is_still_waited(
    panel: FakePanel, client: PanelClient, naps: Naps
) -> None:
    panel.override("POST /invites/check", rate_limited(10))

    await client.check_invite(CODE)

    assert naps.delays == [10]


@pytest.mark.parametrize("retry_after", [11, 60, None])
async def test_a_long_or_unknown_rate_limit_is_not_waited_out(
    panel: FakePanel, client: PanelClient, naps: Naps, retry_after: int | None
) -> None:
    panel.override("POST /invites/check", rate_limited(retry_after))

    with pytest.raises(RateLimited):
        await client.check_invite(CODE)

    assert naps.delays == [] and panel.count("POST /invites/check") == 1


# --- переадресации ---------------------------------------------------------------------------------------------------


async def test_redirects_are_not_followed_so_the_key_never_leaves(panel: FakePanel, client: PanelClient) -> None:
    panel.override("POST /heartbeat", redirect("https://evil.example.net/steal"))

    with pytest.raises(Exception) as caught:
        await client.heartbeat("tollwire_test_bot", __version__)

    assert panel.count("POST /heartbeat") == 1 and len(panel.calls) == 1  # на новый адрес бот не пошёл
    assert not isinstance(caught.value, (PanelAuthError, Rejected, Conflict))


# --- настоящий TLS ---------------------------------------------------------------------------------------------------

requires_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="нужен openssl")


def issue_certificate(folder: Path, name: str = "panel") -> tuple[Path, Path]:
    """Самоподписанный сертификат той же командой, что в install.sh (issue_self_signed) для домена localhost."""
    cert, key = folder / f"{name}.crt", folder / f"{name}.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-days", "825", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ],  # fmt: skip
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},  # Git Bash на Windows иначе превращает «/CN=…» в путь
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


@dataclass
class TlsPanel:
    port: int
    requests: list[str]

    @property
    def url(self) -> str:
        return f"https://localhost:{self.port}"


async def serve_tls(cert: Path, key: Path, requests: list[str]) -> tuple[asyncio.Server, TlsPanel]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            length = next(
                (int(line.split(":")[1]) for line in head.split("\r\n") if line.lower().startswith("content-length")), 0
            )
            if length:
                await reader.readexactly(length)
            requests.append(head)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    return server, TlsPanel(server.sockets[0].getsockname()[1], requests)


@pytest.fixture
async def tls_panel(tmp_path: Path) -> AsyncIterator[tuple[TlsPanel, Path]]:
    cert, key = issue_certificate(tmp_path)
    server, panel = await serve_tls(cert, key, [])
    try:
        yield panel, cert
    finally:
        server.close()
        await server.wait_closed()


@requires_openssl
async def test_own_certificate_is_trusted_through_the_ca_file(tls_panel: tuple[TlsPanel, Path], naps: Naps) -> None:
    panel, cert = tls_panel
    client = PanelClient(panel.url, SECRET_KEY, ca_file=str(cert), sleep=naps)
    try:
        await client.heartbeat("tollwire_test_bot", __version__)
    finally:
        await client.aclose()

    (head,) = panel.requests
    assert head.startswith(f"POST {API_PREFIX}/heartbeat HTTP/1.1")
    assert SECRET_KEY in head  # ключ ушёл серверу, проверенному по сертификату


@requires_openssl
async def test_without_the_ca_file_a_self_signed_panel_is_not_trusted(
    tls_panel: tuple[TlsPanel, Path], naps: Naps
) -> None:
    """Проверка сертификата не отключается: самоподписанная панель без PANEL_CA_FILE - это PanelUnavailable."""
    panel, _ = tls_panel
    client = PanelClient(panel.url, SECRET_KEY, sleep=naps)
    try:
        with pytest.raises(PanelUnavailable):
            await client.heartbeat("tollwire_test_bot", __version__)
    finally:
        await client.aclose()

    assert panel.requests == []  # ключ до недоверенного сервера не дошёл


@requires_openssl
async def test_a_different_ca_file_does_not_make_the_panel_trusted(
    tls_panel: tuple[TlsPanel, Path], tmp_path: Path, naps: Naps
) -> None:
    panel, _ = tls_panel
    other_cert, _ = issue_certificate(tmp_path, "other")
    client = PanelClient(panel.url, SECRET_KEY, ca_file=str(other_cert), sleep=naps)
    try:
        with pytest.raises(PanelUnavailable):
            await client.heartbeat("tollwire_test_bot", __version__)
    finally:
        await client.aclose()

    assert panel.requests == []


@requires_openssl
async def test_the_panel_can_be_addressed_by_ip_when_the_certificate_has_it(
    tls_panel: tuple[TlsPanel, Path], naps: Naps
) -> None:
    """install.sh для адреса-IP кладёт в сертификат IP:..., и проверка имени по IP проходит."""
    panel, cert = tls_panel
    client = PanelClient(f"https://127.0.0.1:{panel.port}", SECRET_KEY, ca_file=str(cert), sleep=naps)
    try:
        await client.heartbeat("tollwire_test_bot", __version__)
    finally:
        await client.aclose()

    assert len(panel.requests) == 1


@requires_openssl
def test_the_test_certificate_is_made_by_the_installer_command() -> None:
    """Страховка от расхождения: если install.sh выпускает сертификат иначе, тест выше проверяет не то."""
    script = (Path(__file__).resolve().parents[2] / "install.sh").read_text(encoding="utf-8")
    fragments = (
        "openssl req -x509",
        "-newkey ec",
        "ec_paramgen_curve:prime256v1",
        "-nodes",
        '-days "$SELF_SIGNED_DAYS"',
        '-subj "/CN=$domain"',
        "subjectAltName=$san",
        'san="DNS:localhost,IP:127.0.0.1"',
    )
    for fragment in fragments:
        assert fragment in script, fragment
