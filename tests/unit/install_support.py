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
BASE_FILES = {"install.sh", "panel", ".env.prod.example", "docker-compose.prod.yml", "deploy", ".gitignore"}

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) [ -z "${FAKE_DOCKER_DOWN:-}" ] || exit 1 ;;
  volume) printf '%s' "${FAKE_VOLUMES:-}" ;;
  inspect) echo "${FAKE_API_HEALTH:-healthy}" ;;
  image) [ -z "${FAKE_NO_IMAGES:-}" ] || exit 1 ;; # docker image inspect: образы панели есть на машине
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
            postgres) [ -n "${FAKE_PG_DOWN:-}" ] || echo postgres-id ;;
          esac
        fi
        ;;
      exec)
        # exec -T postgres sh -c '<команда>': различаем pg_dump и pg_restore по тексту команды
        shift
        while [ "$1" = "-T" ]; do shift; done
        shift # имя сервиса
        script=$3
        case "$script" in
          pg_dump*)
            [ -z "${FAKE_DUMP_FAIL:-}" ] || { echo "pg_dump: error" >&2; exit 1; }
            [ -n "${FAKE_DUMP_EMPTY:-}" ] || printf 'PGDMP-fake-dump:%s\n' "${FAKE_DUMP_BODY:-database}"
            ;;
          pg_restore*)
            cat > "${FAKE_RESTORE_SINK:-/dev/null}"
            [ -z "${FAKE_RESTORE_FAIL:-}" ] || { echo "pg_restore: error" >&2; exit 1; }
            ;;
        esac
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


FAKE_SUDO = """#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_DOCKER_LOG"
exec "$@"
"""

# Настоящий certbot по --standalone слушал бы порт 80; заглушка делает видимое снаружи: запускает хуки так же, как
# он (pre → выпуск → deploy → post) и кладёт «выпущенный» сертификат в $LE_LIVE_DIR/tollwire.
FAKE_CERTBOT = """#!/usr/bin/env bash
export MSYS_NO_PATHCONV=1
echo "certbot $*" >> "$FAKE_DOCKER_LOG"
args=("$@")
dry=0; pre=""; post=""; deploy=""; domain=""
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --dry-run) dry=1 ;;
    --pre-hook) pre=${args[$((i + 1))]} ;;
    --post-hook) post=${args[$((i + 1))]} ;;
    --deploy-hook) deploy=${args[$((i + 1))]} ;;
    -d) domain=${args[$((i + 1))]} ;;
  esac
done
[ "${args[0]}" != renew ] || { echo "renewal check"; exit "${FAKE_RENEW_EXIT:-0}"; }
[ -z "$pre" ] || "$pre"
if [ -n "${FAKE_CERTBOT_FAIL:-}" ] && { [ -z "${FAKE_CERTBOT_FAIL_REAL_ONLY:-}" ] || [ "$dry" -eq 0 ]; }; then
  [ -z "$post" ] || "$post"
  echo "Challenge failed for domain $domain" >&2
  exit 1
fi
if [ "$dry" -eq 0 ]; then
  live="$LE_LIVE_DIR/tollwire"
  mkdir -p "$live"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 90 \
    -subj "/O=Fake Let's Encrypt/CN=$domain" -addext "subjectAltName=DNS:$domain" \
    -keyout "$live/privkey.pem" -out "$live/fullchain.pem" > /dev/null 2>&1
  if [ -z "${FAKE_CERTBOT_NO_DEPLOY:-}" ] && [ -n "$deploy" ]; then RENEWED_LINEAGE="$live" "$deploy"; fi
fi
[ -z "$post" ] || "$post"
exit 0
"""

FAKE_APT_GET = """#!/usr/bin/env bash
echo "apt-get $*" >> "$FAKE_DOCKER_LOG"
case " $* " in
  *" install "*)
    [ -z "${FAKE_APT_FAIL:-}" ] || exit 100
    cp "$(dirname "$0")/.certbot-real" "$(dirname "$0")/certbot"
    chmod +x "$(dirname "$0")/certbot"
    ;;
esac
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
    for name in ("install.sh", "panel", ".env.prod.example", "docker-compose.prod.yml", ".gitignore"):
        shutil.copy(ROOT / name, target / name)
    (target / "deploy").mkdir()
    for name in ("common.sh", "letsencrypt.sh"):
        shutil.copy(ROOT / "deploy" / name, target / "deploy" / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # certbot есть сразу; тест «не установлен» удаляет bin/certbot, apt-get-заглушка вернёт его из .certbot-real.
    (bin_dir / ".certbot-real").write_bytes(FAKE_CERTBOT.encode())
    for name, body in (
        ("docker", FAKE_DOCKER),
        ("curl", FAKE_CURL),
        ("sudo", FAKE_SUDO),
        ("certbot", FAKE_CERTBOT),
        ("apt-get", FAKE_APT_GET),
    ):
        fake = bin_dir / name
        fake.write_bytes(body.encode())  # байты: переводов строки Windows в shebang быть не должно
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return target


def base_env(app: Path) -> dict[str, str]:
    """PATH с заглушками и каталоги Let's Encrypt во временной папке: /etc настоящей машины тесты не трогают."""
    return {
        **os.environ,
        "PATH": f"{app.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(app.parent / "docker.log"),
        "TOLLWIRE_LE_DIR": str(app.parent / "le-hooks"),
        "LE_LIVE_DIR": str(app.parent / "le-live"),
        "TOLLWIRE_LE_HTTP_PORT": str(free_port()),
        "PANEL_LANG": "ru",  # тексты проверяются по-русски; язык меняют тесты в разделе «язык сообщений»
    }


def run(app: Path, *args: str, start: bool = False, **env: str) -> subprocess.CompletedProcess[str]:
    """start=False добавляет --no-start: тесты проверки окружения, секретов и сертификата не запускают панель."""
    if not start:
        args = (*args, "--no-start")
    environment = {**base_env(app), **env}
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


def run_panel(app: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """./panel в копии каталога. Язык русский, как у run(); порты — заведомо свободные: panel запускает install.sh."""
    environment = {**base_env(app), "HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port()), **env}
    return subprocess.run(
        [str(BASH), "panel", *args],
        cwd=app,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )


def installed(app: Path) -> Path:
    """Каталог «уже установленной» панели: есть .env.prod (остальное установщик догонит сам)."""
    (app / ".env.prod").write_text("PANEL_DOMAIN=localhost\n", encoding="utf-8")
    return app


def docker_calls(app: Path) -> list[str]:
    log = app.parent / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []
