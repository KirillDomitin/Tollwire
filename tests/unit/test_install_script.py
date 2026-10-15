"""install.sh (этап 21.1): проверка окружения и создание .env.prod.

Скрипт запускается по-настоящему (bash), но вместо Docker — заглушка в PATH, а работа идёт в копии каталога во
временной папке: настоящий .env.prod и тома разработчика не затрагиваются."""

import re
import shutil
import stat
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values

from core.config import Settings
from tests.unit.install_support import BASE_FILES, ROOT, env_values, free_port, requires_bash, run

pytestmark = requires_bash

SECRET_KEYS = ("POSTGRES_PASSWORD", "JWT_SECRET_KEY", "ENCRYPTION_KEY")


# --- создание .env.prod ---------------------------------------------------------------------------------------------


def test_creates_env_with_fresh_valid_secrets(app: Path) -> None:
    result = run(app, "--domain", "panel.example.com")

    assert result.returncode == 0, result.stderr
    values = env_values(app)
    assert len(values["POSTGRES_PASSWORD"] or "") == 64 and set(values["POSTGRES_PASSWORD"] or "") <= set(
        "0123456789abcdef"
    )
    Fernet((values["ENCRYPTION_KEY"] or "").encode())  # настоящий ключ Fernet: иначе приложение упадёт при шифровании
    assert values["PANEL_DOMAIN"] == "panel.example.com"
    assert len({values[key] for key in SECRET_KEYS}) == 3  # три разных значения
    assert not any("change_me" in (value or "") for value in values.values())


def test_generated_env_is_accepted_by_the_application(app: Path) -> None:
    """Приложение не стартует с заглушкой или коротким JWT-секретом — файл установщика обязан его пройти."""
    run(app, "--domain", "localhost")
    values = {key.lower(): value for key, value in env_values(app).items() if key.lower() in Settings.model_fields}

    settings = Settings(**values, database_url="postgresql+asyncpg://x", redis_url="redis://x")  # type: ignore[arg-type]

    assert len(settings.jwt_secret_key.get_secret_value()) >= 32


def test_rest_of_the_template_is_kept(app: Path) -> None:
    run(app)
    template = dotenv_values(app / ".env.prod.example")
    values = env_values(app)
    different = {key for key in template if template[key] != values.get(key)}
    assert different - {"PANEL_DOMAIN"} == set(SECRET_KEYS)  # меняются только секреты (и, если задан, домен)
    assert values["JWT_ACCESS_EXPIRE"] == template["JWT_ACCESS_EXPIRE"] and values["LOGIN_MAX_FAILURES"] == "5"
    assert "ENCRYPTION_KEY нельзя менять" in (app / ".env.prod").read_text(encoding="utf-8")  # пояснения не потеряны


@pytest.mark.skipif(sys.platform == "win32", reason="права POSIX")
def test_env_is_private(app: Path) -> None:
    run(app)
    assert stat.S_IMODE((app / ".env.prod").stat().st_mode) == 0o600


def test_every_run_makes_different_secrets(app: Path, tmp_path: Path) -> None:
    run(app)
    second = tmp_path / "second"
    shutil.copytree(app, second, ignore=shutil.ignore_patterns(".env.prod"))
    run(second)

    assert env_values(app)["ENCRYPTION_KEY"] != env_values(second)["ENCRYPTION_KEY"]


def test_secrets_are_never_printed(app: Path) -> None:
    result = run(app)
    values = env_values(app)

    for key in SECRET_KEYS:
        assert values[key] and values[key] not in result.stdout + result.stderr
    assert "ENCRYPTION_KEY" in result.stdout  # зато сказано, что его надо сохранить отдельно


# --- существующий .env.prod неприкосновенен -------------------------------------------------------------------------


def test_existing_env_is_left_untouched(app: Path) -> None:
    """Новый ENCRYPTION_KEY сделал бы нечитаемыми все SSH-учётки и VPN-конфиги в базе."""
    original = b"POSTGRES_PASSWORD=mine\nENCRYPTION_KEY=precious\n"
    (app / ".env.prod").write_bytes(original)

    result = run(app, "--domain", "other.example.com")

    assert result.returncode == 0
    assert (app / ".env.prod").read_bytes() == original
    assert "уже" in result.stdout
    assert "precious" not in result.stdout + result.stderr
    assert {path.name for path in app.iterdir()} == BASE_FILES | {".env.prod", "certs"}


def test_data_volume_without_env_stops_the_install(app: Path) -> None:
    """Том базы остался от прошлой установки, а .env.prod потерян: новые пароль БД и ключ шифрования не подойдут."""
    result = run(app, FAKE_VOLUMES="vpn-panel_pg_data\n")

    assert result.returncode != 0
    assert not (app / ".env.prod").exists()
    assert "vpn-panel_pg_data" in result.stderr


def test_data_volume_is_fine_when_env_exists(app: Path) -> None:
    (app / ".env.prod").write_text("POSTGRES_PASSWORD=mine\n", encoding="utf-8")
    assert run(app, FAKE_VOLUMES="vpn-panel_pg_data\n").returncode == 0  # обычное повторное выполнение


# --- проверка окружения ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("variable", "needle"), [("FAKE_DOCKER_DOWN", "Docker"), ("FAKE_NO_COMPOSE", "compose")])
def test_missing_docker_parts_stop_the_install(app: Path, variable: str, needle: str) -> None:
    result = run(app, **{variable: "1"})

    assert result.returncode != 0 and needle in result.stderr
    assert not (app / ".env.prod").exists()


def test_template_without_a_secret_stops_the_install(app: Path) -> None:
    """Шаблон разошёлся с установщиком: лучше отказ, чем файл с заглушкой в ключе шифрования."""
    template = app / ".env.prod.example"
    template.write_text(
        "".join(
            line
            for line in template.read_text(encoding="utf-8").splitlines(True)
            if not line.startswith("ENCRYPTION_KEY=")
        ),
        encoding="utf-8",
    )

    result = run(app)

    assert result.returncode != 0 and "ENCRYPTION_KEY" in result.stderr
    assert not (app / ".env.prod").exists()
    assert {path.name for path in app.iterdir()} == BASE_FILES


# --- домен ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("domain", ["localhost", "panel.example.com", "203.0.113.5", "vpn-1.example.org"])
def test_good_domains_are_accepted(app: Path, domain: str) -> None:
    assert run(app, "--domain", domain).returncode == 0
    assert env_values(app)["PANEL_DOMAIN"] == domain


@pytest.mark.parametrize("domain", ["bad domain", "a;b", "-lead.com", "trail-", "a..b", "$(id)", "a" * 300, "", "x\ny"])
def test_bad_domains_are_rejected_before_anything_is_written(app: Path, domain: str) -> None:
    result = run(app, "--domain", domain)

    assert result.returncode != 0 and "домен" in result.stderr.lower()
    assert not (app / ".env.prod").exists()


def test_without_a_terminal_the_domain_defaults_to_localhost(app: Path) -> None:
    run(app)  # stdin закрыт: спрашивать некого
    assert env_values(app)["PANEL_DOMAIN"] == "localhost"


# --- сам файл -------------------------------------------------------------------------------------------------------


def test_script_has_unix_line_endings_and_a_shebang() -> None:
    """Под Windows git мог бы выдать CRLF — тогда `#!/usr/bin/env bash\\r` не запустится на Linux."""
    data = (ROOT / "install.sh").read_bytes()
    assert data.startswith(b"#!/usr/bin/env bash\n") and b"\r" not in data


def test_help_does_not_touch_anything(app: Path) -> None:
    result = run(app, "--help")

    assert result.returncode == 0 and "--domain" in result.stdout
    assert not (app / ".env.prod").exists()


# --- язык сообщений -------------------------------------------------------------------------------------------------

NO_LOCALE = {"PANEL_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": ""}


def test_messages_are_english_by_default(app: Path) -> None:
    result = run(app, "--domain", "panel.example.com", **NO_LOCALE)

    assert result.returncode == 0, result.stderr
    assert "Created .env.prod" in result.stdout and "IMPORTANT: keep a copy" in result.stdout
    assert not re.search("[А-Яа-я]", result.stdout + result.stderr)


def test_messages_follow_the_system_locale(app: Path) -> None:
    result = run(app, "--domain", "panel.example.com", **{**NO_LOCALE, "LANG": "ru_RU.UTF-8"})

    assert "Создан .env.prod" in result.stdout


def test_locale_variables_have_the_usual_priority(app: Path) -> None:
    """LC_ALL важнее LC_MESSAGES и LANG, как везде в Unix."""
    result = run(app, "--domain", "panel.example.com", **{**NO_LOCALE, "LC_ALL": "en_US.UTF-8", "LANG": "ru_RU.UTF-8"})

    assert "Created .env.prod" in result.stdout


def test_panel_lang_beats_the_locale(app: Path) -> None:
    result = run(app, "--domain", "panel.example.com", **{**NO_LOCALE, "LANG": "ru_RU.UTF-8", "PANEL_LANG": "en"})

    assert "Created .env.prod" in result.stdout


def test_errors_and_usage_are_translated(app: Path) -> None:
    bad = run(app, "--domain", "bad domain", **NO_LOCALE)
    assert bad.returncode != 0 and bad.stderr.startswith("Error: invalid domain")

    together = run(
        app, "--cert", "x", **NO_LOCALE
    )  # сообщение начинается с «--»: printf не должен принять его за параметр
    assert together.returncode != 0 and "--cert and --key must be given together" in together.stderr

    english_help = run(app, "--help", **NO_LOCALE)
    russian_help = run(app, "--help")
    assert "Usage: ./install.sh" in english_help.stdout and "Использование: ./install.sh" in russian_help.stdout
    assert "PANEL_LANG" in english_help.stdout and "PANEL_LANG" in russian_help.stdout


def test_both_languages_have_the_same_messages() -> None:
    """Каждый ключ есть и в русской, и в английской таблице, а число подстановок %s у них совпадает."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")

    def table(name: str) -> dict[str, str]:
        body = text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
        return dict(re.findall(r"^\s+(\w+)\) printf '%s' \"(.*)\" ;;$", body, re.M))

    ru, en = table("msg_ru"), table("msg_en")
    assert ru.keys() == en.keys() and len(ru) > 50
    assert {key: ru[key].count("%s") for key in ru} == {key: en[key].count("%s") for key in en}


def test_admin_step_speaks_the_installers_language(app: Path) -> None:
    ports = {"HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port())}
    ru = run(app, "--domain", "panel.example.com", start=True, **ports)
    en = run(app, "--domain", "panel.example.com", start=True, **{**NO_LOCALE, **ports, "PANEL_LANG": "en"})

    assert "-e PANEL_LANG=ru api python -m scripts.create_admin" in ru.stdout
    assert "-e PANEL_LANG=en api python -m scripts.create_admin" in en.stdout
