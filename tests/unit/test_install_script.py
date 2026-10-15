"""install.sh (этап 21.1): проверка окружения и создание .env.prod.

Скрипт запускается по-настоящему (bash), но вместо Docker — заглушка в PATH, а работа идёт в копии каталога во
временной папке: настоящий .env.prod и тома разработчика не затрагиваются."""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values

from core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not shutil.which("openssl"), reason="нужны bash и openssl")

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) [ -z "${FAKE_DOCKER_DOWN:-}" ] || exit 1 ;;
  compose) [ -z "${FAKE_NO_COMPOSE:-}" ] || exit 1; echo "Docker Compose version v2.29.0" ;;
  volume) printf '%s' "${FAKE_VOLUMES:-}" ;;
esac
"""

SECRET_KEYS = ("POSTGRES_PASSWORD", "JWT_SECRET_KEY", "ENCRYPTION_KEY")


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """Копия того, что нужно установщику: сам скрипт и шаблон. Заглушка docker — в bin/."""
    target = tmp_path / "app"
    target.mkdir()
    for name in ("install.sh", ".env.prod.example"):
        shutil.copy(ROOT / name, target / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_bytes(FAKE_DOCKER.encode())  # байты: переводов строки Windows в shebang быть не должно
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return target


def run(app: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{app.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(app.parent / "docker.log"),
        **env,
    }
    return subprocess.run(
        [str(BASH), "install.sh", *args],
        cwd=app,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def env_values(app: Path) -> dict[str, str | None]:
    return dotenv_values(app / ".env.prod")


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
    assert sorted(path.name for path in app.iterdir()) == [".env.prod", ".env.prod.example", "install.sh"]  # без мусора


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
    assert sorted(path.name for path in app.iterdir()) == [".env.prod.example", "install.sh"]


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
