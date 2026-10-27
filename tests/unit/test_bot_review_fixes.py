"""Исправления по ревью этапа 25.2: режим исполняемого файла в git, игнорирование секретов бота, Compose старше 2.24 для
всех команд, адрес репозитория в команде для второго сервера, curl не читает ~/.curlrc."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.install_support import (
    BASE_FILES,
    BASH,
    BOT_TOKEN,
    ROOT,
    docker_calls,
    free_port,
    installed,
    requires_bash,
    run,
    run_panel,
)

pytestmark = requires_bash


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def in_a_repository() -> bool:
    try:
        return git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except FileNotFoundError:
        return False


requires_git = pytest.mark.skipif(not in_a_repository(), reason="нужен git-репозиторий")


@requires_git
@pytest.mark.parametrize("script", ["install.sh", "panel", "bot/install-bot.sh", "deploy/ship-images.sh"])
def test_scripts_people_run_are_executable_in_git(script: str) -> None:
    """Команда для второго сервера начинается с `./bot/install-bot.sh` сразу после git clone: без бита +x это
    Permission denied (на Windows файл выглядит исполняемым всегда, поэтому смотрим индекс, а не диск)."""
    mode = git("ls-files", "-s", script).stdout.split()[0]

    assert mode == "100755", f"{script} is {mode} in the git index: git update-index --chmod=+x {script}"


@requires_git
@pytest.mark.parametrize(
    "path", [".env.bot", ".env.bot.AbC123", ".env.prod.AbC123", "bot/.env", "bot/.env.AbC123", "bot/certs/panel-ca.pem"]
)
def test_secrets_and_temporary_files_of_the_bot_are_not_committable(path: str) -> None:
    ignored = git("check-ignore", "-q", path)

    assert ignored.returncode == 0, f"{path} is not ignored: git add -A would commit it"


@requires_git
def test_the_env_example_of_the_bot_is_still_tracked() -> None:
    assert git("check-ignore", "-q", "bot/.env.example").returncode == 1


def test_an_old_compose_stops_the_installer_for_everyone(app: Path) -> None:
    """compose-файл панели содержит env_file с required (Compose 2.24+): на старом падает любая команда, и лучше
    сказать об этом прямо, чем показать загадочную ошибку схемы."""
    result = run(app, "--domain", "panel.example.com", "--no-admin", FAKE_COMPOSE_VERSION="2.23.3")

    assert result.returncode != 0 and "2.24" in result.stderr
    assert {path.name for path in app.iterdir()} == BASE_FILES


def test_an_old_compose_stops_panel_commands_too(app: Path) -> None:
    result = run_panel(installed(app), "logs", "--no-follow", FAKE_COMPOSE_VERSION="2.23.3")

    assert result.returncode != 0 and "2.24" in result.stderr


def test_an_unreadable_compose_version_is_not_treated_as_old(app: Path) -> None:
    """Сборка dev и подобное: версию не прочесть - не повод отказывать."""
    result = run(app, "--domain", "panel.example.com", "--no-admin", FAKE_COMPOSE_VERSION="dev-build")

    assert "2.24" not in result.stderr


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("ssh://git@github.com:2222/acme/tollwire.git", "https://github.com/acme/tollwire.git"),
        ("ssh://git@github.com/acme/tollwire.git", "https://github.com/acme/tollwire.git"),
        ("http://example.org/acme/tollwire.git", "https://example.org/acme/tollwire.git"),
        ("https://example.org:8443/acme/tollwire.git", "https://example.org:8443/acme/tollwire.git"),
    ],
    ids=["ssh with a port", "ssh", "plain http", "https with a port"],
)
def test_the_repository_address_keeps_no_ssh_port_and_no_credentials(origin: str, expected: str) -> None:
    script = "source deploy/common.sh; source deploy/bot.sh; bot_repo_url"
    result = subprocess.run(
        [str(BASH), "-c", script],
        cwd=ROOT,
        env={**os.environ, "TOLLWIRE_REPO_URL": origin, "PANEL_LANG": "en"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.org/a;rm -rf x",
        "https://example.org/$(id)",
        "https://example.org/`id`",
        "https://example.org/a b",
    ],
    ids=["semicolon", "command substitution", "backticks", "space"],
)
def test_an_address_with_shell_metacharacters_is_not_printed_into_the_command(origin: str) -> None:
    """Команду человек копирует в терминал второго сервера: метасимволы оболочки в ней недопустимы."""
    script = "source deploy/common.sh; source deploy/bot.sh; bot_repo_url"
    result = subprocess.run(
        [str(BASH), "-c", script],
        cwd=ROOT,
        env={**os.environ, "TOLLWIRE_REPO_URL": origin, "PANEL_LANG": "en"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode != 0 and result.stdout == ""


def test_curl_ignores_the_users_curlrc(app: Path) -> None:
    """~/.curlrc с insecure, proxy или cacert молча отключил бы проверку токена и пиннинг сертификата."""
    environment = {"HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port()), "INSTALL_WAIT_SECONDS": "10"}
    run(
        app,
        "--domain",
        "panel.example.com",
        "--no-admin",
        "--bot",
        "local",
        start=True,
        TELEGRAM_BOT_TOKEN=BOT_TOKEN,
        **environment,
    )

    curls = [line for line in docker_calls(app) if line.startswith("curl") and "/health" not in line]
    assert curls and all(line.startswith("curl -q ") for line in curls)
