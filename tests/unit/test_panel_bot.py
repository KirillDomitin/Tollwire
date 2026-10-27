"""./panel bot (этап 25.2): enable, disable, key, status, invite - на сервере панели.

Скрипт запускается по-настоящему (bash) в копии каталога; Docker и curl - заглушки (`install_support.py`). Проверяется
то же, что у install.sh: токен и ключ не попадают в вывод и аргументы, `.env.bot` приватный, остальные строки
`.env.prod` не меняются, а ещё порядок смены ключа: новый ключ записан, бот перезапущен и виден панели - и только
потом старый отозван (иначе неудачная смена оставила бы бота без ключа)."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.install_support import (
    BOT_TOKEN,
    ROOT,
    base_env,
    bot_events,
    docker_calls,
    env_bot,
    fingerprint_of,
    installed,
    issued_keys,
    positions,
    requires_bash,
    requires_posix,
    run_in_terminal,
    run_panel,
    transcript,
)

pytestmark = requires_bash


REQUIRED = ("deploy/bot.sh",)
# Тесты, которые верны и до реализации (защита от регрессии): без бота установщик ведёт себя как раньше.
REGRESSION = set()


@pytest.fixture(autouse=True)
def _bot_scripts_exist(request: pytest.FixtureRequest) -> None:
    """Отказы и «ничего не записано» выполнялись бы и без скриптов бота: пока файлов нет, тест красный."""
    if request.node.name.split("[")[0] in REGRESSION:
        return
    missing = [path for path in REQUIRED if not (ROOT / path).exists()]
    assert not missing, f"бот ещё не реализован (шаг 8 плана 25.2), нет файлов: {missing}"


BOT_UP = re.compile(r" up .*-d.* bot$")


@pytest.fixture
def app(app: Path) -> Path:
    return installed(app)


def enable(app: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return run_panel(app, "bot", "enable", **{"TELEGRAM_BOT_TOKEN": BOT_TOKEN, "INSTALL_WAIT_SECONDS": "10", **env})


def prod_lines(app: Path) -> list[str]:
    return (app / ".env.prod").read_text(encoding="utf-8").splitlines()


def profiles(app: Path) -> str | None:
    found = [line.split("=", 1)[1] for line in prod_lines(app) if line.startswith("COMPOSE_PROFILES=")]
    return found[0] if found else None


def bot_up_calls(app: Path) -> list[str]:
    return [line for line in docker_calls(app) if line.startswith("docker compose") and BOT_UP.search(line)]


def key_calls(app: Path, command: str) -> list[str]:
    return [line for line in docker_calls(app) if f"scripts.bot_key {command}" in line]


# --- enable ----------------------------------------------------------------------------------------------------------


def test_enable_checks_the_token_issues_a_key_writes_the_env_and_starts_the_bot(app: Path) -> None:
    before = (app / ".env.prod").read_text(encoding="utf-8")

    result = enable(app)

    assert result.returncode == 0, result.stderr
    (key,) = issued_keys(app)
    assert env_bot(app)["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN and env_bot(app)["BOT_API_KEY"] == key
    assert profiles(app) == "bot"
    assert [line for line in prod_lines(app) if not line.startswith("COMPOSE_PROFILES")] == before.splitlines()
    assert [event.split()[0] for event in bot_events(app)[:2]] == ["getMe", "created"]
    (up,) = bot_up_calls(app)
    calls = docker_calls(app)
    create, started, waited = positions(calls, "scripts.bot_key create", up, "scripts.bot_key seen")
    assert create < started < waited and "--within 90" in calls[waited]


def test_enable_keeps_secrets_out_of_the_output_the_arguments_and_the_panel_env(app: Path) -> None:
    result = enable(app)

    (key,) = issued_keys(app)
    seen = transcript(app, result)
    assert BOT_TOKEN not in seen and key not in seen
    prod = (app / ".env.prod").read_text(encoding="utf-8")
    assert BOT_TOKEN not in prod and key not in prod


@pytest.mark.skipif(sys.platform == "win32", reason="права POSIX")
def test_enable_makes_a_private_env_file(app: Path) -> None:
    enable(app)

    assert (app / ".env.bot").stat().st_mode & 0o777 == 0o600
    assert [path.name for path in app.iterdir() if path.name.startswith(".env.bot.")] == []


def test_enable_with_an_existing_env_file_only_turns_the_bot_on(app: Path) -> None:
    enable(app)
    run_panel(app, "bot", "disable")
    env_before = (app / ".env.bot").read_bytes()

    result = run_panel(app, "bot", "enable", INSTALL_WAIT_SECONDS="10")  # токена нет, и спрашивать не нужно

    assert result.returncode == 0, result.stderr
    assert len(issued_keys(app)) == 1 and len(bot_events(app, "getMe")) == 1
    assert (app / ".env.bot").read_bytes() == env_before and profiles(app) == "bot"
    assert len(bot_up_calls(app)) == 2


def test_enable_twice_does_not_change_anything(app: Path) -> None:
    enable(app)
    env_before, prod_before = (app / ".env.bot").read_bytes(), (app / ".env.prod").read_bytes()

    again = run_panel(app, "bot", "enable", INSTALL_WAIT_SECONDS="10")

    assert again.returncode == 0, again.stderr
    assert len(issued_keys(app)) == 1
    assert (app / ".env.bot").read_bytes() == env_before and (app / ".env.prod").read_bytes() == prod_before


def test_enable_without_a_token_and_a_terminal_says_what_is_missing(app: Path) -> None:
    result = run_panel(app, "bot", "enable")

    assert result.returncode != 0 and "TELEGRAM_BOT_TOKEN" in result.stderr
    assert not (app / ".env.bot").exists() and issued_keys(app) == [] and profiles(app) is None


@pytest.mark.parametrize(
    "failure", [{"FAKE_TELEGRAM_FAIL": "1"}, {"FAKE_TELEGRAM_BAD_TOKEN": BOT_TOKEN}], ids=["unreachable", "wrong token"]
)
def test_enable_with_a_token_telegram_refuses_configures_nothing(app: Path, failure: dict[str, str]) -> None:
    result = enable(app, **failure)

    assert result.returncode != 0 and "Telegram" in result.stdout + result.stderr
    assert not (app / ".env.bot").exists() and issued_keys(app) == [] and profiles(app) is None
    assert bot_up_calls(app) == [] and BOT_TOKEN not in transcript(app, result)


@pytest.mark.parametrize("token", ["abc", "12345:short", "7000000001:AAF3x9Qk1ZpLm2NvB8cD4eR6tY0uI5o P7aS"])
def test_enable_refuses_a_malformed_token_without_calling_curl(app: Path, token: str) -> None:
    result = run_panel(app, "bot", "enable", TELEGRAM_BOT_TOKEN=token)

    assert result.returncode != 0
    assert bot_events(app, "getMe") == [] and not (app / ".env.bot").exists() and issued_keys(app) == []


def test_enable_with_an_old_compose_is_refused(app: Path) -> None:
    result = enable(app, FAKE_COMPOSE_VERSION="2.23.1")

    assert result.returncode != 0 and "2.24" in result.stderr
    assert not (app / ".env.bot").exists() and issued_keys(app) == [] and profiles(app) is None


def test_a_silent_bot_gets_a_hint_to_read_the_logs(app: Path) -> None:
    result = enable(app, FAKE_BOT_SILENT="1", INSTALL_WAIT_SECONDS="3")

    assert "./panel logs bot" in result.stdout + result.stderr
    assert (app / ".env.bot").exists() and profiles(app) == "bot"  # настройка сохранена: чинить можно без повторной


def test_a_failed_bot_start_is_reported(app: Path) -> None:
    result = enable(app, FAKE_BOT_UP_FAIL="1")

    assert result.returncode != 0 and "logs bot" in result.stdout + result.stderr
    assert BOT_TOKEN not in transcript(app, result)


# --- disable ---------------------------------------------------------------------------------------------------------


def test_disable_stops_and_removes_the_container_and_drops_the_profile(app: Path) -> None:
    enable(app)

    result = run_panel(app, "bot", "disable")

    assert result.returncode == 0, result.stderr
    calls = docker_calls(app)
    stop, remove = positions(calls, " stop bot", " rm -f bot")
    assert stop < remove and "bot" not in (profiles(app) or "").split(",")
    assert bot_events(app, "stop_bot") and bot_events(app, "rm_bot")


def test_disable_keeps_the_env_file_and_does_not_revoke_the_key(app: Path) -> None:
    enable(app)
    env_before = (app / ".env.bot").read_bytes()

    result = run_panel(app, "bot", "disable")

    assert (app / ".env.bot").read_bytes() == env_before
    assert bot_events(app, "revoked") == [] and key_calls(app, "revoke") == []
    assert "ключ" in result.stdout.lower()  # сказано, что ключ остаётся действующим и как его отозвать


def test_disable_keeps_other_profiles(app: Path) -> None:
    enable(app)
    (app / ".env.prod").write_text(
        (app / ".env.prod").read_text(encoding="utf-8").replace("COMPOSE_PROFILES=bot", "COMPOSE_PROFILES=extra,bot"),
        encoding="utf-8",
    )

    run_panel(app, "bot", "disable")

    assert profiles(app) == "extra"


def test_disable_is_harmless_when_the_bot_was_never_enabled_or_already_disabled(app: Path) -> None:
    assert run_panel(app, "bot", "disable").returncode == 0

    enable(app)
    assert run_panel(app, "bot", "disable").returncode == 0
    assert run_panel(app, "bot", "disable").returncode == 0


# --- key -------------------------------------------------------------------------------------------------------------


def test_key_rotation_replaces_the_key_restarts_the_bot_and_only_then_revokes_the_old_one(app: Path) -> None:
    enable(app)
    (old,) = issued_keys(app)

    result = run_panel(app, "bot", "key", INSTALL_WAIT_SECONDS="10")

    assert result.returncode == 0, result.stderr
    old_key, new = issued_keys(app)
    assert old_key == old and new != old
    assert env_bot(app)["BOT_API_KEY"] == new and env_bot(app)["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    events = [event.split()[0] for event in bot_events(app)]
    # getMe, created(old) | created(new), up_bot(new), seen_check(new), revoked(old)
    assert events[-4:] == ["created", "up_bot", "seen_check", "revoked"]
    (revoked,) = bot_events(app, "revoked")
    assert revoked == f"revoked {old} env={new}"  # в момент отзыва старого в .env.bot уже новый
    assert any("--force-recreate" in line for line in bot_up_calls(app))  # бот перезапущен, а не оставлен со старым
    assert old not in transcript(app, result) and new not in transcript(app, result)


def test_the_old_key_is_not_revoked_when_the_new_one_was_never_seen(app: Path) -> None:
    enable(app)
    (old,) = issued_keys(app)

    result = run_panel(app, "bot", "key", FAKE_BOT_SILENT="1", INSTALL_WAIT_SECONDS="3")

    assert bot_events(app, "revoked") == []  # старый ключ ещё может понадобиться, если новый не заработал
    assert "./panel logs bot" in result.stdout + result.stderr
    assert old not in transcript(app, result)


def test_key_rotation_needs_an_enabled_bot(app: Path) -> None:
    result = run_panel(app, "bot", "key")

    assert result.returncode != 0 and "./panel bot enable" in result.stderr
    assert issued_keys(app) == []


def test_a_failed_key_issue_changes_nothing(app: Path) -> None:
    enable(app)
    env_before = (app / ".env.bot").read_bytes()

    result = run_panel(app, "bot", "key", FAKE_BOT_KEY_FAIL="1")

    assert result.returncode != 0
    assert (app / ".env.bot").read_bytes() == env_before and bot_events(app, "revoked") == []


def test_remote_key_is_not_shown_without_a_terminal(app: Path) -> None:
    result = run_panel(app, "bot", "key", "--remote")

    assert result.returncode != 0
    assert issued_keys(app) == [] and "twb_" not in transcript(app, result)


@requires_posix
def test_remote_key_in_a_terminal_is_shown_once_and_the_local_bot_is_untouched(app: Path) -> None:
    enable(app)
    env_before = (app / ".env.bot").read_bytes()

    code, output = run_in_terminal(app, ["panel", "bot", "key", "--remote"])

    assert code == 0, output
    _, new = issued_keys(app)
    assert output.count(new) == 1  # один раз
    assert (app / ".env.bot").read_bytes() == env_before  # локальный бот продолжает работать со своим ключом
    assert new not in "\n".join(docker_calls(app))
    assert bot_events(app, "revoked") == []


# --- status, invite --------------------------------------------------------------------------------------------------


def test_status_asks_the_panel_for_the_keys(app: Path) -> None:
    result = run_panel(app, "bot", "status")

    assert result.returncode == 0, result.stderr
    assert "FAKE-KEY-LIST" in result.stdout
    assert len(key_calls(app, "list")) == 1


def test_invite_hands_the_arguments_to_the_script_and_shows_the_link(app: Path) -> None:
    result = run_panel(app, "bot", "invite", "create", "--protocol", "P-1", "--days", "30", "--expires-in", "7d")

    assert result.returncode == 0, result.stderr
    assert "https://t.me/fake_bot?start=" in result.stdout
    (call,) = [line for line in docker_calls(app) if "scripts.bot_invite" in line]
    assert call.endswith("scripts.bot_invite create --protocol P-1 --days 30 --expires-in 7d")
    assert bot_events(app, "invite_created")


def test_invite_protocols_is_available_too(app: Path) -> None:
    result = run_panel(app, "bot", "invite", "protocols")

    assert result.returncode == 0, result.stderr
    assert "FAKE-PROTOCOL-LIST" in result.stdout


# --- общее -----------------------------------------------------------------------------------------------------------


def test_bot_without_a_subcommand_lists_them(app: Path) -> None:
    result = run_panel(app, "bot")

    text = result.stdout + result.stderr
    assert all(word in text for word in ("enable", "disable", "key", "status", "invite"))


def test_unknown_bot_subcommand_is_named(app: Path) -> None:
    result = run_panel(app, "bot", "frobnicate")

    assert result.returncode != 0 and "frobnicate" in result.stderr
    assert not any("scripts.bot_" in line for line in docker_calls(app))


def test_the_main_help_mentions_the_bot_commands(app: Path) -> None:
    result = run_panel(app, "help")

    assert result.returncode == 0 and "bot" in result.stdout and "enable" in result.stdout


def test_the_bot_logs_can_be_read_like_any_service(app: Path) -> None:
    result = run_panel(app, "logs", "bot", "--tail", "5", "--no-follow")

    assert result.returncode == 0, result.stderr
    assert docker_calls(app)[-1].endswith("logs --tail 5 bot")


def test_messages_are_english_when_asked(app: Path) -> None:
    result = run_panel(app, "bot", "frobnicate", PANEL_LANG="en")

    assert result.returncode != 0 and "frobnicate" in result.stderr
    assert not re.search("[А-Яа-я]", result.stderr)


def test_cert_status_shows_the_fingerprint_the_remote_bot_will_be_asked_for(app: Path) -> None:
    (app / "certs").mkdir()
    environment = {**base_env(app), "MSYS_NO_PATHCONV": "1"}
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
            "30",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(app / "certs" / "privkey.pem"),
            "-out",
            str(app / "certs" / "fullchain.pem"),
        ],  # fmt: skip
        env=environment,
        check=True,
        capture_output=True,
        timeout=30,
    )

    result = run_panel(app, "cert", "status")

    assert result.returncode == 0, result.stderr
    assert fingerprint_of(app / "certs" / "fullchain.pem") in result.stdout.upper()
