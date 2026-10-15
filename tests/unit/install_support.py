"""Общее для тестов install.sh: запуск скрипта в копии каталога с заглушкой Docker (настоящий .env.prod, certs/ и
тома разработчика не затрагиваются)."""

import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(not BASH or not shutil.which("openssl"), reason="нужны bash и openssl")
# Что лежит в каталоге установки до запуска; всё остальное создал установщик.
BASE_FILES = {"install.sh", ".env.prod.example", "docker-compose.prod.yml"}

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) [ -z "${FAKE_DOCKER_DOWN:-}" ] || exit 1 ;;
  volume) printf '%s' "${FAKE_VOLUMES:-}" ;;
  inspect) echo "${FAKE_API_HEALTH:-healthy}" ;;
  compose)
    shift
    if [ "$1" = version ]; then
      [ -z "${FAKE_NO_COMPOSE:-}" ] || exit 1
      echo "Docker Compose version v2.29.0"
      exit 0
    fi
    while [ "$1" = "--env-file" ] || [ "$1" = "-f" ]; do shift 2; done
    case "$1" in
      up)
        env=0; cert=0
        [ ! -f .env.prod ] || env=1
        [ ! -f certs/fullchain.pem ] || cert=1
        echo "UP env=$env cert=$cert" >> "$FAKE_DOCKER_LOG"
        [ -z "${FAKE_UP_FAIL:-}" ] || { echo "build failed" >&2; exit 1; }
        ;;
      ps)
        if [ "$2" = "-q" ]; then
          case "$3" in
            nginx) [ -z "${FAKE_RUNNING:-}" ] || echo nginx-id ;;
            api) echo api-id ;;
          esac
        fi
        ;;
      logs) echo "log line" ;;
    esac
    ;;
esac
"""

FAKE_CURL = """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_DOCKER_LOG"
[ -z "${FAKE_CURL_FAIL:-}" ] || exit 22
"""


def free_port() -> int:
    """Заведомо свободный порт: тест запуска не должен зависеть от того, что слушает машина разработчика."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def prepare_app(tmp_path: Path) -> Path:
    """Копия того, что нужно установщику: сам скрипт и шаблон. Заглушка docker — в соседнем bin/."""
    target = tmp_path / "app"
    target.mkdir()
    for name in ("install.sh", ".env.prod.example", "docker-compose.prod.yml"):
        shutil.copy(ROOT / name, target / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        fake = bin_dir / name
        fake.write_bytes(body.encode())  # байты: переводов строки Windows в shebang быть не должно
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return target


def run(app: Path, *args: str, start: bool = False, **env: str) -> subprocess.CompletedProcess[str]:
    """start=False добавляет --no-start: тесты проверки окружения, секретов и сертификата не запускают панель."""
    if not start:
        args = (*args, "--no-start")
    environment = {
        **os.environ,
        "PATH": f"{app.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "PANEL_LANG": "ru",  # тексты проверяются по-русски; язык меняют тесты в разделе «язык сообщений»
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
