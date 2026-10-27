"""Бот на другом сервере (этап 25.2): `install.sh --bot remote` на сервере панели и `bot/install-bot.sh` на втором.

Скрипты запускаются по-настоящему (bash) в копии каталога; Docker и curl - заглушки (`install_support.py`). TLS
настоящий там, где он важен: `openssl s_client` из install-bot.sh ходит на TLS-сервер в процессе теста с
сертификатом, выпущенным той же командой openssl, что и в install.sh.

Главное, что ловят тесты: ключ бота виден один раз и только человеку в терминале, в команду для другого сервера он не
вставляется; отпечаток сертификата сверяется до того, как бот поверит самоподписанной панели; секреты не попадают ни в
вывод, ни в аргументы команд; при любом отказе `bot/.env` не записывается."""

import os
import re
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.unit.install_support import (
    BASE_FILES,
    BOT_TOKEN,
    ROOT,
    bot_events,
    docker_calls,
    fingerprint_of,
    free_port,
    issued_keys,
    requires_bash,
    requires_posix,
    run,
    run_bot_installer,
    run_in_terminal,
    transcript,
)

pytestmark = requires_bash


REQUIRED = ("deploy/bot.sh", "bot/install-bot.sh")
# Тесты, которые верны и до реализации (защита от регрессии): без бота установщик ведёт себя как раньше.
REGRESSION = set()


@pytest.fixture(autouse=True)
def _bot_scripts_exist(request: pytest.FixtureRequest) -> None:
    """Отказы и «ничего не записано» выполнялись бы и без скриптов бота: пока файлов нет, тест красный."""
    if request.node.name.split("[")[0] in REGRESSION:
        return
    missing = [path for path in REQUIRED if not (ROOT / path).exists()]
    assert not missing, f"бот ещё не реализован (шаг 8 плана 25.2), нет файлов: {missing}"


REMOTE_KEY = "twb_" + "Zx9" * 14 + "Z"  # форма ключа бота: twb_ и 43 символа; значение выдуманное
PANEL = "https://panel.example.com"
DOMAIN = ("--domain", "panel.example.com")
BOT_UP = re.compile(r"-f bot/docker-compose\.yml.* up .*-d")


def install(app: Path, *args: str, url: str = PANEL, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {"BOT_API_KEY": REMOTE_KEY, "TELEGRAM_BOT_TOKEN": BOT_TOKEN, "INSTALL_WAIT_SECONDS": "10", **env}
    return run_bot_installer(app, "--panel-url", url, *args, **environment)


def bot_env(app: Path) -> dict[str, str]:
    values = {}
    for line in (app / "bot" / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            name, value = line.split("=", 1)
            values[name] = value
    return values


def nothing_written(app: Path) -> bool:
    return not (app / "bot" / ".env").exists() and not any(BOT_UP.search(line) for line in docker_calls(app))


def heartbeat_events(app: Path) -> list[str]:
    return bot_events(app, "heartbeat")


# --- install-bot.sh: путь без сбоев ----------------------------------------------------------------------------------


def test_the_script_is_part_of_the_repository() -> None:
    assert (ROOT / "bot" / "install-bot.sh").is_file()


def test_install_checks_the_token_then_the_key_writes_the_env_and_starts_the_bot(app: Path) -> None:
    result = install(app)

    assert result.returncode == 0, result.stderr
    env = bot_env(app)
    assert env["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN and env["BOT_API_KEY"] == REMOTE_KEY and env["PANEL_URL"] == PANEL
    assert [event.split()[0] for event in bot_events(app)[:2]] == ["getMe", "heartbeat"]
    assert bot_events(app, "getMe") == [f"getMe {BOT_TOKEN}"]
    (heartbeat,) = heartbeat_events(app)
    assert f"key={REMOTE_KEY}" in heartbeat and f"url={PANEL}/api/v1/bot/heartbeat" in heartbeat
    assert "install" in heartbeat and "fake_bot" in heartbeat  # version=install и имя бота из getMe
    calls = docker_calls(app)
    up = next(index for index, line in enumerate(calls) if BOT_UP.search(line) and "--build" in line)
    assert up > max(index for index, line in enumerate(calls) if line.startswith("curl") and "/health" not in line)
    assert any("ps -q bot" in line for line in calls) and any("inspect" in line and "bot-id" in line for line in calls)
    assert bot_events(app, "up_bot")[0].startswith("up_bot env=bot/.env")


def test_secrets_stay_out_of_the_output_and_the_arguments(app: Path) -> None:
    result = install(app)

    seen = transcript(app, result)
    assert BOT_TOKEN not in seen and REMOTE_KEY not in seen


@pytest.mark.skipif(os.name != "posix", reason="права POSIX")
def test_the_env_file_is_private_and_no_temp_files_stay(app: Path) -> None:
    install(app)

    assert (app / "bot" / ".env").stat().st_mode & 0o777 == 0o600
    assert [path.name for path in (app / "bot").iterdir() if path.name.startswith(".env.")] == [".env.example"]


def test_a_certificate_the_system_trusts_needs_no_ca_file(app: Path) -> None:
    install(app)

    assert "PANEL_CA_FILE" not in bot_env(app) and not (app / "bot" / "certs" / "panel-ca.pem").exists()


@pytest.mark.parametrize("missing", ["BOT_API_KEY", "TELEGRAM_BOT_TOKEN"])
def test_a_missing_secret_is_named_and_nothing_is_written(app: Path, missing: str) -> None:
    result = install(app, **{missing: ""})

    assert result.returncode != 0 and missing in result.stderr
    assert nothing_written(app)


# --- адрес панели ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://panel.example.com", "http://api:8000", "http://10.0.0.5", "ftp://panel.example.com", "panel.example.com"],
    ids=["http", "http internal name", "http ip", "ftp", "no scheme"],
)
def test_only_https_panels_are_accepted(app: Path, url: str) -> None:
    result = install(app, url=url)

    assert result.returncode != 0 and "https" in result.stderr
    assert nothing_written(app) and bot_events(app) == []


@pytest.mark.parametrize(
    "url",
    [
        'https://panel.example.com" -o /tmp/x',
        "https://panel example.com",
        "https://panel.example.com\\evil",
        "https://user:secret@panel.example.com",
    ],
    ids=["quote", "space", "backslash", "userinfo"],
)
def test_an_address_that_could_smuggle_curl_options_is_refused(app: Path, url: str) -> None:
    """Адрес попадает в конфиг curl (`url = "…"`); кавычка, пробел или userinfo недопустимы."""
    result = install(app, url=url)

    assert result.returncode != 0
    assert nothing_written(app) and bot_events(app) == []
    assert "secret" not in transcript(app, result)


def test_a_missing_panel_url_is_named(app: Path) -> None:
    result = run_bot_installer(app, BOT_API_KEY=REMOTE_KEY, TELEGRAM_BOT_TOKEN=BOT_TOKEN)

    assert result.returncode != 0 and "--panel-url" in result.stderr
    assert nothing_written(app)


# --- отказы до записи ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure", [{"FAKE_TELEGRAM_FAIL": "1"}, {"FAKE_TELEGRAM_BAD_TOKEN": BOT_TOKEN}], ids=["unreachable", "wrong token"]
)
def test_a_token_telegram_refuses_stops_before_the_panel_is_asked(app: Path, failure: dict[str, str]) -> None:
    result = install(app, **failure)

    assert result.returncode != 0 and "Telegram" in result.stderr
    assert nothing_written(app) and heartbeat_events(app) == []
    assert BOT_TOKEN not in transcript(app, result)


def test_a_rejected_key_is_named_and_nothing_is_written(app: Path) -> None:
    result = install(app, FAKE_PANEL_STATUS="401")

    assert result.returncode != 0 and "неверный или отозван" in result.stderr
    assert nothing_written(app)
    assert REMOTE_KEY not in transcript(app, result)


@pytest.mark.parametrize("secret", [{"BOT_API_KEY": "not-a-key"}, {"TELEGRAM_BOT_TOKEN": "not-a-token"}])
def test_malformed_secrets_never_reach_curl(app: Path, secret: dict[str, str]) -> None:
    result = install(app, **secret)

    assert result.returncode != 0
    assert nothing_written(app) and heartbeat_events(app) == []


def test_an_old_compose_is_refused(app: Path) -> None:
    result = install(app, FAKE_COMPOSE_VERSION="2.23.1")

    assert result.returncode != 0 and "2.24" in result.stderr
    assert nothing_written(app) and bot_events(app) == []


@pytest.mark.parametrize("problem", ["FAKE_DOCKER_DOWN", "FAKE_NO_COMPOSE"])
def test_missing_docker_or_compose_is_reported_first(app: Path, problem: str) -> None:
    result = install(app, **{problem: "1"})

    assert result.returncode != 0 and nothing_written(app) and bot_events(app) == []


def test_a_bot_that_never_gets_healthy_is_reported_with_the_logs_hint(app: Path) -> None:
    result = install(app, FAKE_BOT_HEALTH="starting", INSTALL_WAIT_SECONDS="2")

    assert result.returncode != 0 and "logs" in result.stderr
    assert BOT_TOKEN not in transcript(app, result) and REMOTE_KEY not in transcript(app, result)


def test_a_failed_start_is_reported_with_the_logs_hint(app: Path) -> None:
    result = install(app, FAKE_BOT_UP_FAIL="1")

    assert result.returncode != 0 and "logs" in result.stderr


def test_unknown_options_are_refused(app: Path) -> None:
    result = install(app, "--frobnicate")

    assert result.returncode != 0 and "--frobnicate" in result.stderr
    assert nothing_written(app)


def test_help_lists_the_options(app: Path) -> None:
    result = run_bot_installer(app, "--help")

    assert result.returncode == 0
    assert all(option in result.stdout for option in ("--panel-url", "--panel-fingerprint", "--reconfigure"))


# --- сертификат панели -----------------------------------------------------------------------------------------------


@dataclass
class TlsPanel:
    port: int
    cert: Path
    fingerprint: str

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"


@pytest.fixture
def tls_panel(tmp_path: Path) -> Iterator[TlsPanel]:
    """Настоящий TLS-сервер с самоподписанным сертификатом - для `openssl s_client -showcerts` из install-bot.sh."""
    cert, key = tmp_path / "panel.crt", tmp_path / "panel.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "825",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],  # fmt: skip
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        check=True,
        capture_output=True,
        timeout=30,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve() -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            try:
                with context.wrap_socket(client, server_side=True) as secured:
                    secured.settimeout(2)
                    secured.recv(1)
            except (OSError, ssl.SSLError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield TlsPanel(listener.getsockname()[1], cert, fingerprint_of(cert))
    finally:
        listener.close()
        thread.join(timeout=5)


def test_a_self_signed_panel_is_trusted_after_the_fingerprint_matches(app: Path, tls_panel: TlsPanel) -> None:
    result = install(app, "--panel-fingerprint", tls_panel.fingerprint, url=tls_panel.url, FAKE_PANEL_TLS_FAIL="1")

    assert result.returncode == 0, result.stderr
    ca = app / "bot" / "certs" / "panel-ca.pem"
    assert fingerprint_of(ca) == tls_panel.fingerprint  # сохранён именно сертификат панели
    (heartbeat,) = heartbeat_events(app)
    assert re.search(r"cacert=\S*panel-ca\.pem", heartbeat)  # и дальше curl проверяет им
    assert bot_env(app)["PANEL_CA_FILE"].endswith("panel-ca.pem")
    assert any(BOT_UP.search(line) for line in docker_calls(app))


def test_a_different_fingerprint_is_refused_and_nothing_is_trusted(app: Path, tls_panel: TlsPanel) -> None:
    wrong = ":".join(["AA"] * 32)

    result = install(app, "--panel-fingerprint", wrong, url=tls_panel.url, FAKE_PANEL_TLS_FAIL="1")

    assert result.returncode != 0 and re.search("отпечат|fingerprint", result.stderr, re.I)
    assert nothing_written(app) and not (app / "bot" / "certs" / "panel-ca.pem").exists()
    assert heartbeat_events(app) == []  # ключ до недоверенной панели не дошёл


def test_without_a_fingerprint_and_a_terminal_a_self_signed_panel_is_not_trusted(
    app: Path, tls_panel: TlsPanel
) -> None:
    result = install(app, url=tls_panel.url, FAKE_PANEL_TLS_FAIL="1")

    assert result.returncode != 0 and "--panel-fingerprint" in result.stderr
    assert nothing_written(app) and not (app / "bot" / "certs" / "panel-ca.pem").exists()


def test_no_certificate_is_fetched_when_the_tls_check_passes(app: Path, tls_panel: TlsPanel) -> None:
    result = install(app, url=tls_panel.url)  # без FAKE_PANEL_TLS_FAIL: curl доверяет панели сам

    assert result.returncode == 0, result.stderr
    assert not (app / "bot" / "certs" / "panel-ca.pem").exists() and "PANEL_CA_FILE" not in bot_env(app)


# --- --reconfigure ---------------------------------------------------------------------------------------------------


def test_reconfigure_changes_only_the_key(app: Path) -> None:
    install(app)
    before = bot_env(app)
    new_key = "twb_" + "Hn4" * 14 + "H"

    result = run_bot_installer(
        app, "--reconfigure", "--panel-url", PANEL, BOT_API_KEY=new_key, INSTALL_WAIT_SECONDS="10"
    )  # токена в окружении нет: он остаётся прежним

    assert result.returncode == 0, result.stderr
    after = bot_env(app)
    assert after["BOT_API_KEY"] == new_key
    assert {k: v for k, v in after.items() if k != "BOT_API_KEY"} == {
        k: v for k, v in before.items() if k != "BOT_API_KEY"
    }
    assert new_key in heartbeat_events(app)[-1]  # новый ключ проверен панелью
    assert bot_events(app, "up_bot")[-1].split()[2] == f"key={new_key}"  # и бот перезапущен с ним
    assert new_key not in transcript(app, result) and REMOTE_KEY not in transcript(app, result)


def test_reconfigure_with_a_rejected_key_keeps_the_old_one(app: Path) -> None:
    install(app)
    env_before = (app / "bot" / ".env").read_bytes()

    result = run_bot_installer(
        app, "--reconfigure", "--panel-url", PANEL, BOT_API_KEY="twb_" + "Hn4" * 14 + "H", FAKE_PANEL_STATUS="401"
    )

    assert result.returncode != 0
    assert (app / "bot" / ".env").read_bytes() == env_before


def test_reconfigure_needs_an_installed_bot(app: Path) -> None:
    result = run_bot_installer(app, "--reconfigure", "--panel-url", PANEL, BOT_API_KEY=REMOTE_KEY)

    assert result.returncode != 0 and nothing_written(app)


# --- язык ------------------------------------------------------------------------------------------------------------


def test_messages_are_english_when_asked(app: Path) -> None:
    result = install(app, url="http://panel.example.com", PANEL_LANG="en")

    assert result.returncode != 0 and "https" in result.stderr and not re.search("[А-Яа-я]", result.stderr)


def test_every_message_exists_in_both_languages() -> None:
    text = (ROOT / "bot" / "install-bot.sh").read_text(encoding="utf-8")

    def table(name: str) -> dict[str, str]:
        body = text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
        return dict(re.findall(r"^\s+(\w+)\) printf '%s' \"(.*)\" ;;$", body, re.M))

    ru, en = table("msg_ru"), table("msg_en")
    assert ru.keys() == en.keys() and len(ru) > 10
    assert {key: ru[key].count("%s") for key in ru} == {key: en[key].count("%s") for key in en}


def test_the_script_uses_the_common_library_of_the_repository() -> None:
    assert "deploy/common.sh" in (ROOT / "bot" / "install-bot.sh").read_text(encoding="utf-8")


# --- install.sh --bot remote -----------------------------------------------------------------------------------------


def start(app: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {"HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port()), "INSTALL_WAIT_SECONDS": "10", **env}
    return run(app, *DOMAIN, "--no-admin", *args, start=True, **environment)


def test_remote_without_a_terminal_refuses_to_show_the_key(app: Path) -> None:
    result = start(app, "--bot", "remote")

    assert result.returncode == 0, result.stderr  # панель при этом установлена
    assert "./panel bot key --remote" in result.stdout + result.stderr
    assert "twb_" not in transcript(app, result)
    assert issued_keys(app) == []  # ключ, который некому показать, не выпускается
    assert not (app / ".env.bot").exists() and "COMPOSE_PROFILES" not in (app / ".env.prod").read_text(encoding="utf-8")
    assert "Панель запущена" in result.stdout


def test_remote_does_not_touch_the_local_bot_setup(app: Path) -> None:
    result = start(app, "--bot", "remote")

    assert result.returncode == 0, result.stderr
    assert not any(BOT_UP.search(line) or line.endswith(" bot") for line in docker_calls(app))
    assert {path.name for path in app.iterdir()} == BASE_FILES | {".env.prod", "certs"}


def git_origin(app: Path, url: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=app, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=app, check=True, capture_output=True)


def remote_in_terminal(app: Path, *answers: str, flag: bool = True, **env: str) -> tuple[int, str, str]:
    """Установка в псевдотерминале. Домен localhost: для публичного install.sh сперва спросил бы про сертификат."""
    port = str(free_port())
    argv = ["install.sh", "--domain", "localhost", "--no-admin", *(["--bot", "remote"] if flag else [])]
    code, output = run_in_terminal(app, argv, answers, HTTPS_PORT=port, INSTALL_WAIT_SECONDS="10", **env)
    return code, output, port


def clone_command(output: str) -> str:
    return next(line for line in output.splitlines() if "install-bot.sh" in line)


@requires_posix
def test_remote_in_a_terminal_prints_the_command_and_shows_the_key_separately(app: Path) -> None:
    git_origin(app, "git@github.com:acme/tollwire.git")

    code, output, port = remote_in_terminal(app)

    assert code == 0, output
    (key,) = issued_keys(app)
    command = clone_command(output)
    assert "git clone https://github.com/acme/tollwire.git tollwire" in command
    assert f"./bot/install-bot.sh --panel-url https://localhost:{port}" in command
    assert f"--panel-fingerprint {fingerprint_of(app / 'certs' / 'fullchain.pem')}" in command
    assert key not in command  # ключ в команду не вставляется: install-bot.sh спросит его скрыто
    assert output.count(key) == 1 and any(
        line.strip() == key for line in output.splitlines()
    )  # один раз и отдельной строкой
    assert key not in "\n".join(docker_calls(app))
    assert not (app / ".env.bot").exists()


@requires_posix
@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("git@github.com:acme/tollwire.git", "https://github.com/acme/tollwire.git"),
        ("ssh://git@github.com/acme/tollwire.git", "https://github.com/acme/tollwire.git"),
        ("https://user:s3cretpass@git.example.org/acme/tollwire.git", "https://git.example.org/acme/tollwire.git"),
        ("https://github.com/acme/tollwire.git", "https://github.com/acme/tollwire.git"),
    ],
    ids=["scp-like", "ssh url", "https with userinfo", "plain https"],
)
def test_the_repository_address_in_the_command_is_https_without_credentials(
    app: Path, origin: str, expected: str
) -> None:
    git_origin(app, origin)

    code, output, _ = remote_in_terminal(app)

    assert code == 0, output
    assert f"git clone {expected} tollwire" in clone_command(output)
    assert "s3cretpass" not in output and "user:" not in clone_command(output)


@requires_posix
def test_the_repository_address_can_be_overridden(app: Path) -> None:
    git_origin(app, "git@github.com:acme/tollwire.git")

    code, output, _ = remote_in_terminal(app, TOLLWIRE_REPO_URL="https://mirror.example.net/t.git")

    assert code == 0, output
    assert "git clone https://mirror.example.net/t.git tollwire" in clone_command(output)


@requires_posix
def test_a_fresh_install_in_a_terminal_can_choose_another_server(app: Path) -> None:
    git_origin(app, "git@github.com:acme/tollwire.git")

    code, output, _ = remote_in_terminal(app, "2", flag=False)

    assert code == 0, output
    (key,) = issued_keys(app)
    assert "install-bot.sh" in output and output.count(key) == 1
    assert not (app / ".env.bot").exists()


@requires_posix
def test_a_rerun_in_a_terminal_does_not_issue_another_key(app: Path) -> None:
    git_origin(app, "git@github.com:acme/tollwire.git")
    remote_in_terminal(app)

    code, output, _ = remote_in_terminal(app, flag=False)

    assert code == 0, output
    assert len(issued_keys(app)) == 1 and "install-bot.sh" not in output
