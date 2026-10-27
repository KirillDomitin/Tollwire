"""install.sh, бот на этом сервере (этап 25.2): флаг `--bot`, токен, ключ, `.env.bot`, профиль compose, heartbeat.

Скрипт запускается по-настоящему (bash) в копии каталога; Docker и curl - заглушки (`install_support.py`: `FakeDocker`
выпускает ключи и «запускает бота», `FakeCurl` читает конфиг из `-K -` и отвечает getMe и heartbeat). Терминала у тестов
нет, поэтому вопрос «Telegram-бот: 1/2/3» проверяется в конце файла в псевдотерминале (только POSIX, в CI - Linux).

Главное, что ловят тесты: токен и ключ не попадают ни в вывод, ни в аргументы команд (`ps`), ни в `.env.prod` (только в
`.env.bot` с правами 0600); остальные строки `.env.prod` не меняются ни на байт; повторный запуск и `./panel update`
бота не трогают; сбой бота не ломает установку панели."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.install_support import (
    BASE_FILES,
    BASH,
    BOT_TOKEN,
    ROOT,
    base_env,
    bot_events,
    docker_calls,
    env_bot,
    free_port,
    issued_keys,
    positions,
    requires_bash,
    requires_posix,
    run,
    run_in_terminal,
    run_panel,
    transcript,
)

pytestmark = requires_bash


REQUIRED = ("deploy/bot.sh",)
# Тесты, которые верны и до реализации (защита от регрессии): без бота установщик ведёт себя как раньше.
REGRESSION = {
    "test_no_build_without_the_profile_does_not_ask_for_the_bot_image",
    "test_without_the_flag_and_without_a_terminal_the_bot_is_not_touched",
}


@pytest.fixture(autouse=True)
def _bot_scripts_exist(request: pytest.FixtureRequest) -> None:
    """Отказы и «ничего не записано» выполнялись бы и без скриптов бота: пока файлов нет, тест красный."""
    if request.node.name.split("[")[0] in REGRESSION:
        return
    missing = [path for path in REQUIRED if not (ROOT / path).exists()]
    assert not missing, f"бот ещё не реализован (шаг 8 плана 25.2), нет файлов: {missing}"


DOMAIN = ("--domain", "panel.example.com")
BOT_UP = re.compile(r" up .*-d.* bot$")


def start(app: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Установка с запуском; ожидание бота короткое, порты заведомо свободные."""
    environment = {"HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port()), "INSTALL_WAIT_SECONDS": "10", **env}
    return run(app, *DOMAIN, "--no-admin", *args, start=True, **environment)


def with_bot(app: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return start(app, "--bot", "local", *args, TELEGRAM_BOT_TOKEN=BOT_TOKEN, **env)


def bot_up_calls(app: Path) -> list[str]:
    return [line for line in docker_calls(app) if line.startswith("docker compose") and BOT_UP.search(line)]


def key_calls(app: Path, command: str) -> list[str]:
    return [line for line in docker_calls(app) if f"scripts.bot_key {command}" in line]


def nothing_configured(app: Path) -> bool:
    """Бот не настроен: нет .env.bot, выпущенных ключей, запуска бота и профиля в .env.prod."""
    prod = (app / ".env.prod").read_text(encoding="utf-8") if (app / ".env.prod").exists() else ""
    return (
        not (app / ".env.bot").exists()
        and issued_keys(app) == []
        and bot_up_calls(app) == []
        and "COMPOSE_PROFILES" not in prod
    )


def untouched_bot(app: Path) -> bool:
    """К боту не прикасались вообще: к тому же токен никто не проверял (нет getMe)."""
    return nothing_configured(app) and bot_events(app, "getMe") == []


# --- флаг --bot ------------------------------------------------------------------------------------------------------


def test_without_the_flag_and_without_a_terminal_the_bot_is_not_touched(app: Path) -> None:
    result = start(app)

    assert result.returncode == 0, result.stderr
    assert untouched_bot(app)


def test_bot_none_does_nothing(app: Path) -> None:
    result = start(app, "--bot", "none", TELEGRAM_BOT_TOKEN=BOT_TOKEN)

    assert result.returncode == 0, result.stderr
    assert untouched_bot(app)
    assert BOT_TOKEN not in transcript(app, result)


@pytest.mark.parametrize("value", ["maybe", "LOCAL", "1", ""])
def test_unknown_bot_choice_is_refused_before_anything_is_written(app: Path, value: str) -> None:
    result = start(app, "--bot", value)

    assert (
        result.returncode != 0 and "--bot" in result.stderr and "remote" in result.stderr
    )  # допустимые значения названы
    assert {path.name for path in app.iterdir()} == BASE_FILES


def test_bot_flag_needs_a_value(app: Path) -> None:
    result = run(app, *DOMAIN, "--bot")

    assert result.returncode != 0 and "--bot" in result.stderr and "remote" in result.stderr
    assert {path.name for path in app.iterdir()} == BASE_FILES


def test_help_describes_the_flag(app: Path) -> None:
    result = run(app, "--help")

    assert result.returncode == 0 and "--bot" in result.stdout
    assert all(word in result.stdout for word in ("local", "remote", "none"))


# --- этот сервер: .env.bot, ключ, профиль ----------------------------------------------------------------------------


def test_local_bot_gets_a_key_and_its_own_env_file(app: Path) -> None:
    result = with_bot(app)

    assert result.returncode == 0, result.stderr
    (key,) = issued_keys(app)
    values = env_bot(app)
    assert values["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN and values["BOT_API_KEY"] == key
    assert len(key_calls(app, "create")) == 1 and "--raw" in key_calls(app, "create")[0]


def test_the_token_is_checked_with_get_me_before_a_key_is_issued(app: Path) -> None:
    with_bot(app)

    events = bot_events(app)
    assert events[0] == f"getMe {BOT_TOKEN}"
    assert [event.split()[0] for event in events[:2]] == ["getMe", "created"]


def test_curl_gets_the_token_through_stdin_not_arguments(app: Path) -> None:
    result = with_bot(app)

    calls = [line for line in docker_calls(app) if line.startswith("curl") and "/health" not in line]
    assert calls, "the token was never checked"
    assert all(re.search(r"(-K|--config) -", line) for line in calls)
    assert BOT_TOKEN not in transcript(app, result)


def test_the_profile_is_added_to_env_prod_and_the_other_lines_stay_byte_for_byte(app: Path) -> None:
    start(app, "--bot", "none")
    before = (app / ".env.prod").read_bytes()

    result = with_bot(app)

    assert result.returncode == 0, result.stderr
    after = (app / ".env.prod").read_bytes()
    kept = [line for line in after.splitlines(keepends=True) if not line.startswith(b"COMPOSE_PROFILES")]
    assert b"".join(kept) == before
    assert after.splitlines().count(b"COMPOSE_PROFILES=bot") == 1


def test_existing_profiles_are_kept(app: Path) -> None:
    start(app, "--bot", "none")
    with (app / ".env.prod").open("a", encoding="utf-8") as env:
        env.write("COMPOSE_PROFILES=extra\n")

    with_bot(app)

    profiles = [
        line for line in (app / ".env.prod").read_text(encoding="utf-8").splitlines() if line.startswith("COMPOSE_")
    ]
    assert profiles == ["COMPOSE_PROFILES=extra,bot"]


def test_bot_secrets_never_reach_the_panel_env_or_the_output(app: Path) -> None:
    result = with_bot(app)

    (key,) = issued_keys(app)
    prod = (app / ".env.prod").read_text(encoding="utf-8")
    assert BOT_TOKEN not in prod and key not in prod
    assert "TELEGRAM_BOT_TOKEN" not in prod and "BOT_API_KEY" not in prod  # api, worker и migrate их не получают
    seen = transcript(app, result)
    assert BOT_TOKEN not in seen and key not in seen


@requires_posix
def test_env_bot_is_private(app: Path) -> None:
    with_bot(app)

    assert (app / ".env.bot").stat().st_mode & 0o777 == 0o600


@requires_posix
def test_env_bot_is_private_even_under_a_permissive_umask_and_leaves_no_temp_files(app: Path) -> None:
    environment = {**base_env(app), "HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port())}
    environment.update(TELEGRAM_BOT_TOKEN=BOT_TOKEN, INSTALL_WAIT_SECONDS="10")

    subprocess.run(
        [str(BASH), "install.sh", *DOMAIN, "--no-admin", "--bot", "local"],
        cwd=app,
        env=environment,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        preexec_fn=lambda: os.umask(0),  # noqa: PLW1509
    )

    assert (app / ".env.bot").stat().st_mode & 0o777 == 0o600
    assert (app / ".env.prod").stat().st_mode & 0o777 == 0o600
    assert [path.name for path in app.iterdir() if path.name.startswith(".env.bot.")] == []


def test_the_bot_is_started_and_waited_for_after_the_key_exists(app: Path) -> None:
    result = with_bot(app)

    assert result.returncode == 0, result.stderr
    (up,) = bot_up_calls(app)
    assert "--build" in up and "--env-file .env.prod" in up
    calls = docker_calls(app)
    create, started, waited = positions(calls, "scripts.bot_key create", up, "scripts.bot_key seen")
    assert create < started < waited
    assert "--within 90" in calls[waited]
    (key,) = issued_keys(app)
    assert f"seen_check {key}" in bot_events(app)[-1] and bot_events(app, "up_bot")[0].split()[2] == f"key={key}"


def test_a_bot_that_answers_gets_no_complaint(app: Path) -> None:
    result = with_bot(app)

    assert result.returncode == 0 and (app / ".env.bot").exists()
    assert "logs bot" not in result.stdout + result.stderr


def test_a_silent_bot_gets_a_hint_but_the_installation_survives(app: Path) -> None:
    result = with_bot(app, FAKE_BOT_SILENT="1", INSTALL_WAIT_SECONDS="3")

    assert result.returncode == 0, result.stderr
    assert "./panel logs bot" in result.stdout + result.stderr
    assert (app / ".env.bot").exists()  # настройка сохранена, чинить можно без повторной установки
    assert "Панель запущена" in result.stdout


# --- сбои ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure", [{"FAKE_TELEGRAM_FAIL": "1"}, {"FAKE_TELEGRAM_BAD_TOKEN": BOT_TOKEN}], ids=["unreachable", "wrong token"]
)
def test_a_failed_get_me_warns_and_configures_nothing(app: Path, failure: dict[str, str]) -> None:
    result = with_bot(app, **failure)

    assert bot_events(app, "getMe") == [f"getMe {BOT_TOKEN}"]  # токен проверяли...
    assert nothing_configured(app)  # ...и ничего не настроено
    assert "Telegram" in result.stdout + result.stderr  # человеку сказано, что с токеном или с сетью
    assert "Панель запущена" in result.stdout  # панель при этом установлена
    assert BOT_TOKEN not in transcript(app, result)


@pytest.mark.parametrize(
    "token",
    [
        "abc",
        "12345:short",
        "7000000001:AAF3x9Qk1ZpLm2NvB8cD4eR6tY0uI5oP7a S",
        '7000000001:AAF3x9Qk1ZpLm2NvB8cD4eR6tY0uI5o"S',
    ],
    ids=["no colon", "short", "space", "quote"],
)
def test_a_malformed_token_never_reaches_curl(app: Path, token: str) -> None:
    """Токен попадает в конфиг curl (`url = "…"`): кавычка или пробел дали бы возможность дописать свои директивы."""
    result = start(app, "--bot", "local", TELEGRAM_BOT_TOKEN=token)

    assert bot_events(app, "getMe") == [] and untouched_bot(app)
    assert "Панель запущена" in result.stdout


@requires_posix
def test_a_token_with_a_newline_cannot_inject_curl_options(app: Path) -> None:
    injected = BOT_TOKEN + '"\nurl = "file:///etc/passwd'

    start(app, "--bot", "local", TELEGRAM_BOT_TOKEN=injected)

    assert bot_events(app, "getMe") == [] and untouched_bot(app)


def test_without_a_token_and_a_terminal_the_bot_step_says_what_is_missing(app: Path) -> None:
    result = start(app, "--bot", "local")

    assert "TELEGRAM_BOT_TOKEN" in result.stdout + result.stderr
    assert untouched_bot(app) and "Панель запущена" in result.stdout


def test_a_failed_key_issue_leaves_the_bot_unconfigured(app: Path) -> None:
    result = with_bot(app, FAKE_BOT_KEY_FAIL="1")

    assert nothing_configured(app)
    assert "ключ" in (result.stdout + result.stderr).lower()
    assert "Панель запущена" in result.stdout


def test_a_failed_bot_start_points_to_the_logs_and_keeps_the_panel(app: Path) -> None:
    result = with_bot(app, FAKE_BOT_UP_FAIL="1")

    assert "logs bot" in result.stdout + result.stderr
    assert BOT_TOKEN not in transcript(app, result)
    assert "Панель запущена" in result.stdout


def test_an_old_compose_cannot_run_the_bot(app: Path) -> None:
    """`env_file: {required: false}` понимает Compose 2.24+: на старом бот не включается, а не падает при запуске."""
    result = with_bot(app, FAKE_COMPOSE_VERSION="2.23.3")

    assert nothing_configured(app) and "2.24" in result.stdout + result.stderr


# --- повторные запуски -----------------------------------------------------------------------------------------------


def test_rerun_with_the_flag_reuses_the_existing_bot(app: Path) -> None:
    with_bot(app)
    env_before, prod_before = (app / ".env.bot").read_bytes(), (app / ".env.prod").read_bytes()

    again = start(app, "--bot", "local")  # токена в окружении нет: спрашивать нечего

    assert again.returncode == 0, again.stderr
    assert len(issued_keys(app)) == 1 and len(bot_events(app, "getMe")) == 1
    assert (app / ".env.bot").read_bytes() == env_before and (app / ".env.prod").read_bytes() == prod_before


def test_plain_rerun_does_not_touch_the_bot(app: Path) -> None:
    with_bot(app)
    env_before, prod_before = (app / ".env.bot").read_bytes(), (app / ".env.prod").read_bytes()

    again = start(app)

    assert again.returncode == 0, again.stderr
    assert len(issued_keys(app)) == 1 and (app / ".env.bot").read_bytes() == env_before
    assert (app / ".env.prod").read_bytes() == prod_before


def test_panel_update_does_not_touch_the_bot(app: Path) -> None:
    with_bot(app)
    env_before, prod_before = (app / ".env.bot").read_bytes(), (app / ".env.prod").read_bytes()

    result = run_panel(app, "update", "--no-pull", "--no-backup")

    assert result.returncode == 0, result.stderr
    assert len(issued_keys(app)) == 1 and len(bot_events(app, "getMe")) == 1
    assert (app / ".env.bot").read_bytes() == env_before and (app / ".env.prod").read_bytes() == prod_before


def test_no_start_skips_the_bot_and_says_how_to_add_it_later(app: Path) -> None:
    result = run(
        app, *DOMAIN, "--no-admin", "--bot", "local", TELEGRAM_BOT_TOKEN=BOT_TOKEN
    )  # run() добавляет --no-start

    assert result.returncode == 0, result.stderr
    assert untouched_bot(app) and "./panel bot enable" in result.stdout
    assert BOT_TOKEN not in transcript(app, result)


# --- --no-build ------------------------------------------------------------------------------------------------------


def enable_profile(app: Path) -> None:
    start(app, "--bot", "none")
    with (app / ".env.prod").open("a", encoding="utf-8") as env:
        env.write("COMPOSE_PROFILES=bot\n")


def test_no_build_with_the_bot_profile_needs_the_bot_image(app: Path) -> None:
    enable_profile(app)
    before = len(docker_calls(app))

    result = start(app, "--no-build", FAKE_MISSING_IMAGES="vpn-panel-bot:latest")

    assert result.returncode != 0
    assert "vpn-panel-bot:latest" in result.stderr and "ship-images.sh" in result.stderr
    assert not any(
        line.endswith(" up -d") for line in docker_calls(app)[before:]
    )  # compose не лезет за образом в реестр


def test_no_build_with_the_bot_profile_and_the_image_starts(app: Path) -> None:
    enable_profile(app)

    result = start(app, "--no-build")

    assert result.returncode == 0, result.stderr
    assert any("image inspect vpn-panel-bot:latest" in line for line in docker_calls(app))  # образ бота проверен


def test_no_build_without_the_profile_does_not_ask_for_the_bot_image(app: Path) -> None:
    result = start(app, "--no-build", FAKE_MISSING_IMAGES="vpn-panel-bot:latest")

    assert result.returncode == 0, result.stderr


# --- язык ------------------------------------------------------------------------------------------------------------


def test_bot_step_messages_are_english_when_asked(app: Path) -> None:
    result = start(app, "--bot", "local", PANEL_LANG="en", FAKE_TELEGRAM_FAIL="1", TELEGRAM_BOT_TOKEN=BOT_TOKEN)

    output = result.stdout + result.stderr
    assert "Telegram" in output and not re.search("[А-Яа-я]", output)


def test_every_bot_message_exists_in_both_languages() -> None:
    text = (ROOT / "deploy" / "bot.sh").read_text(encoding="utf-8")

    def table(name: str) -> dict[str, str]:
        body = text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
        return dict(re.findall(r"^\s+(\w+)\) printf '%s' \"(.*)\" ;;$", body, re.M))

    ru, en = table("lib_msg_ru"), table("lib_msg_en")
    assert ru.keys() == en.keys() and len(ru) > 10
    assert {key: ru[key].count("%s") for key in ru} == {key: en[key].count("%s") for key in en}


def test_the_library_is_sourced_by_both_scripts() -> None:
    """Общий код бота живёт в deploy/bot.sh и подключается из install.sh и panel (а не копируется)."""
    for name in ("install.sh", "panel"):
        assert "deploy/bot.sh" in (ROOT / name).read_text(encoding="utf-8"), name


def test_env_set_replaces_one_line_atomically_and_keeps_the_rest(app: Path, tmp_path: Path) -> None:
    """`env_set` из deploy/common.sh: заменяет одну строку, остальные байт в байт; файл остаётся 0600."""
    target = tmp_path / "sample.env"
    target.write_bytes(b"# comment\nA=1\nCOMPOSE_PROFILES=old\nB=two words\n")
    target.chmod(0o600)

    script = f"source deploy/common.sh; ENV_FILE={target.as_posix()}; env_set COMPOSE_PROFILES bot; env_set NEW value"
    subprocess.run([str(BASH), "-c", script], cwd=app, env=base_env(app), check=True, capture_output=True)

    assert target.read_bytes() == b"# comment\nA=1\nCOMPOSE_PROFILES=bot\nB=two words\nNEW=value\n"
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600


# --- вопрос в терминале (только POSIX) -------------------------------------------------------------------------------


def terminal_install(app: Path, *answers: str, **env: str) -> tuple[int, str]:
    """Домен localhost: для публичного домена install.sh сперва спросил бы про сертификат (меню 1/2/3)."""
    argv = ["install.sh", "--domain", "localhost", "--no-admin"]
    return run_in_terminal(app, argv, answers, INSTALL_WAIT_SECONDS="10", **env)


@requires_posix
def test_a_fresh_install_in_a_terminal_asks_and_enter_means_no_bot(app: Path) -> None:
    code, output = terminal_install(app, "")

    assert code == 0, output
    assert "Telegram" in output  # вопрос про бота был
    assert untouched_bot(app)


@requires_posix
def test_choosing_this_server_asks_for_the_token_hidden_and_installs_the_bot(app: Path) -> None:
    code, output = terminal_install(app, "1", BOT_TOKEN)

    assert code == 0, output
    (key,) = issued_keys(app)
    assert env_bot(app)["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN and env_bot(app)["BOT_API_KEY"] == key
    assert BOT_TOKEN not in output  # ввод скрытый: на экране его нет
    assert key not in output


@requires_posix
def test_a_second_run_in_a_terminal_does_not_ask_again(app: Path) -> None:
    terminal_install(app, "")

    code, output = terminal_install(app, "1", BOT_TOKEN)

    assert code == 0, output
    assert untouched_bot(app)  # ответы не прочитаны: вопроса не было
