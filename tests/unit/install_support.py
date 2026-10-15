"""Общее для тестов install.sh: запуск скрипта в копии каталога с заглушкой Docker (настоящий .env.prod, certs/ и
тома разработчика не затрагиваются)."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(not BASH or not shutil.which("openssl"), reason="нужны bash и openssl")

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) [ -z "${FAKE_DOCKER_DOWN:-}" ] || exit 1 ;;
  compose) [ -z "${FAKE_NO_COMPOSE:-}" ] || exit 1; echo "Docker Compose version v2.29.0" ;;
  volume) printf '%s' "${FAKE_VOLUMES:-}" ;;
esac
"""


def prepare_app(tmp_path: Path) -> Path:
    """Копия того, что нужно установщику: сам скрипт и шаблон. Заглушка docker — в соседнем bin/."""
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
