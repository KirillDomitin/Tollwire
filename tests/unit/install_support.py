"""Общее для тестов install.sh: запуск скрипта в копии каталога с заглушкой Docker (настоящий .env.prod, certs/ и
тома разработчика не затрагиваются).

Этап 25.2 (бот). Заглушки умеют ровно то, что скрипты бота вызывают:

- `FakeDocker`: `compose … run … scripts.bot_key create --name N --raw` выдаёт ключ `twb_` + 43 цифры (по счётчику);
  `seen`/`revoke` читают ключ со stdin; `compose … up … bot` (или `-f bot/docker-compose.yml up`) «запускает бота» -
  если не задан `FAKE_BOT_SILENT`, бот шлёт heartbeat ключом из `.env.bot` (на сервере панели) или `bot/.env`;
  `compose run` без `-T` при не-терминальном вводе отказывает, как настоящий («the input device is not a TTY»).
  Журнал событий для проверок (в нём ключи и токены можно, в `docker.log` и выводе - нельзя) -
  `$FAKE_BOT_STATE/events.log`, читать через `bot_events()`.
- `FakeCurl`: читает конфиг из `-K -`; на `https://api.telegram.org/bot<токен>/getMe` отвечает JSON бота
  (`FAKE_TELEGRAM_FAIL` - сети нет, exit 7; `FAKE_TELEGRAM_BAD_TOKEN=<токен>` - 401); на `…/api/v1/bot/heartbeat` читает
  заголовок `X-Bot-Key` (`FAKE_PANEL_STATUS=401` - ключ отозван; `FAKE_PANEL_TLS_FAIL` - exit 60, пока нет
  `--cacert`/`-k`).
- `FAKE_MISSING_IMAGES="vpn-panel-bot:latest"` - этих образов на машине нет; `FAKE_COMPOSE_VERSION` - версия Compose;
  `FAKE_BOT_UP_FAIL`, `FAKE_BOT_KEY_FAIL`, `FAKE_BOT_HEALTH` - сбои запуска, выпуска ключа и здоровье контейнера бота.

Принятые допущения для скриптов (план 25.2 их не фиксирует): файл `.env.bot` лежит в корне, в нём `TELEGRAM_BOT_TOKEN`
и `BOT_API_KEY`; ожидание heartbeat бота укладывается в `INSTALL_WAIT_SECONDS`; токен приходит из `TELEGRAM_BOT_TOKEN`,
ключ (install-bot.sh) - из `BOT_API_KEY`; без терминала скрипты ничего не спрашивают.
"""

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
requires_posix = pytest.mark.skipif(os.name != "posix", reason="права POSIX и псевдотерминал (в CI - Linux)")
# Что лежит в каталоге установки до запуска; всё остальное создал установщик.
BASE_FILES = {"install.sh", "panel", ".env.prod.example", "docker-compose.prod.yml", "deploy", ".gitignore"}
# Файлы бота копируются в каталог установки, когда они есть в репозитории (до шага 8 плана 25.2 их нет).
BOT_FILES = ("install-bot.sh", "docker-compose.yml", ".env.example")
BOT_DEPLOY_FILES = ("bot.sh",)
if (ROOT / "bot" / "install-bot.sh").exists():
    BASE_FILES = BASE_FILES | {"bot"}

BOT_TOKEN = "7000000001:AAF3x9Qk1ZpLm2NvB8cD4eR6tY0uI5oP7aS"  # форма токена BotFather; значение выдуманное

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_DOCKER_LOG"
state=${FAKE_BOT_STATE:-/tmp/fake-bot-state-unset}
mkdir -p "$state"
event() { echo "$*" >> "$state/events.log"; }
env_value() { grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2-; }
case "$1" in
  info) [ -z "${FAKE_DOCKER_DOWN:-}" ] || exit 1 ;;
  volume) printf '%s' "${FAKE_VOLUMES:-}" ;;
  inspect)
    case "$*" in
      *bot-id*) echo "${FAKE_BOT_HEALTH:-healthy}" ;;
      *) echo "${FAKE_API_HEALTH:-healthy}" ;;
    esac
    ;;
  image) # docker image inspect <образ>: образы панели есть на машине
    [ -z "${FAKE_NO_IMAGES:-}" ] || exit 1
    case " ${FAKE_MISSING_IMAGES:-} " in *" $3 "*) exit 1 ;; esac
    ;;
  compose)
    shift
    if [ "$1" = version ]; then
      [ -z "${FAKE_NO_COMPOSE:-}" ] || exit 1
      echo "Docker Compose version v${FAKE_COMPOSE_VERSION:-2.29.0}"
      exit 0
    fi
    compose_file=""
    while [ "$1" = "--env-file" ] || [ "$1" = "-f" ] || [ "$1" = "--profile" ]; do
      [ "$1" != "-f" ] || compose_file=$2
      shift 2
    done
    case "$1" in
      up)
        env=0; cert=0
        [ ! -f .env.prod ] || env=1
        [ ! -f certs/fullchain.pem ] || cert=1
        echo "UP env=$env cert=$cert" >> "$FAKE_DOCKER_LOG"
        [ -z "${FAKE_UP_FAIL:-}" ] || { echo "build failed" >&2; exit 1; }
        bot_env=""
        case " $* " in *" bot "*) bot_env=.env.bot ;; esac
        [ "$compose_file" != bot/docker-compose.yml ] || bot_env=bot/.env
        if [ -n "$bot_env" ]; then
          [ -z "${FAKE_BOT_UP_FAIL:-}" ] || { echo "bot build failed" >&2; exit 1; }
          key=$(env_value "$bot_env" BOT_API_KEY)
          event "up_bot env=$bot_env key=$key token=$(env_value "$bot_env" TELEGRAM_BOT_TOKEN)"
          touch "$state/bot_running"
          [ -n "${FAKE_BOT_SILENT:-}" ] || echo "$key" >> "$state/seen"
        fi
        ;;
      stop | rm)
        case " $* " in *" bot "*) rm -f "$state/bot_running"; event "$1_bot" ;; esac
        ;;
      ps)
        if [ "$2" = "-q" ]; then
          case "$3" in
            nginx) [ -z "${FAKE_RUNNING:-}" ] || echo nginx-id ;;
            api) echo api-id ;;
            postgres) [ -n "${FAKE_PG_DOWN:-}" ] || echo postgres-id ;;
            bot) [ ! -f "$state/bot_running" ] || echo bot-id ;;
          esac
        fi
        ;;
      config)
        echo nginx; echo api; echo worker; echo migrate; echo postgres; echo redis
        if grep -q '^COMPOSE_PROFILES=.*bot' .env.prod 2>/dev/null; then echo bot; fi
        ;;
      run)
        # run --rm [-T] [-e …] api python -m scripts.<имя> <подкоманда> …
        found=""; sub=""; rest=""; tty_off=0
        for arg in "$@"; do
          if [ -n "$sub" ]; then rest="$rest $arg"
          elif [ -n "$found" ]; then sub=$arg
          else
            [ "$arg" != -T ] || tty_off=1
            case "$arg" in scripts.bot_key | scripts.bot_invite) found=$arg ;; esac
          fi
        done
        if [ -n "$found" ]; then
          # Настоящий compose run без -T при не-терминальном вводе отказывает.
          if [ "$tty_off" -eq 0 ] && [ ! -t 0 ]; then echo "the input device is not a TTY" >&2; exit 1; fi
          case "$found/$sub" in
            scripts.bot_key/create)
              case "$rest" in *--name*) ;; *) echo "--name is required" >&2; exit 2 ;; esac
              case "$rest" in *--raw*) ;; *) echo "needs a terminal or --raw" >&2; exit 1 ;; esac
              [ -z "${FAKE_BOT_KEY_FAIL:-}" ] || { echo "bot_key failed" >&2; exit 1; }
              n=$(( $(cat "$state/counter" 2>/dev/null || echo 0) + 1 ))
              echo "$n" > "$state/counter"
              key=$(printf 'twb_%043d' "$n")
              event "created $key args:$rest"
              echo "$key"
              ;;
            scripts.bot_key/revoke)
              key=$(cat | tr -d '[:space:]')
              event "revoked $key env=$(env_value .env.bot BOT_API_KEY)"
              echo "$key" >> "$state/revoked"
              ;;
            scripts.bot_key/seen)
              key=$(cat | tr -d '[:space:]')
              event "seen_check $key args:$rest"
              grep -qx "$key" "$state/seen" 2>/dev/null && ! grep -qx "$key" "$state/revoked" 2>/dev/null
              exit $?
              ;;
            scripts.bot_key/list) echo "FAKE-KEY-LIST" ;;
            scripts.bot_invite/protocols) echo "FAKE-PROTOCOL-LIST" ;;
            scripts.bot_invite/create)
              event "invite_created args:$rest"
              echo "https://t.me/fake_bot?start=FakeInvite00000000000001"
              ;;
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

# curl читает конфиг из `-K -` (так токен Telegram и ключ бота не попадают в аргументы и в `ps`) и отвечает по адресу.
FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_DOCKER_LOG"
state=${FAKE_BOT_STATE:-/tmp/fake-bot-state-unset}
mkdir -p "$state"
event() { echo "$*" >> "$state/events.log"; }
config=""
cacert=""
insecure=0
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    -K | --config)
      source=${args[$((i + 1))]}
      if [ "$source" = - ]; then config=$(cat); else config=$(cat "$source" 2>/dev/null || true); fi
      ;;
    --cacert) cacert=${args[$((i + 1))]} ;;
    --insecure | -k | -[A-Za-z]*k*) insecure=1 ;;
  esac
done
# значение директивы curl-конфига: строка вида `имя = "значение"`
config_value() {
  printf '%s\n' "$config" | sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\(.*\)\"[[:space:]]*$/\1/p" | head -n1
}
url=$(config_value url)
if [ -z "$url" ]; then
  for arg in "$@"; do case "$arg" in http*) url=$arg ;; esac; done
fi
case "$url" in
  https://api.telegram.org/bot*/getMe)
    token=${url#https://api.telegram.org/bot}
    token=${token%/getMe}
    event "getMe $token"
    [ -z "${FAKE_TELEGRAM_FAIL:-}" ] || exit 7
    if [ "$token" = "${FAKE_TELEGRAM_BAD_TOKEN:-__none__}" ]; then
      echo '{"ok":false,"error_code":401,"description":"Unauthorized"}'
      exit 22
    fi
    echo '{"ok":true,"result":{"id":42,"is_bot":true,"first_name":"Fake","username":"fake_bot"}}'
    exit 0
    ;;
  */api/v1/bot/heartbeat)
    key=$(config_value header | sed -n 's/^[Xx]-[Bb]ot-[Kk]ey:[[:space:]]*//p')
    body=$(config_value data)
    if [ -n "${FAKE_PANEL_TLS_FAIL:-}" ] && [ -z "$cacert" ] && [ "$insecure" -eq 0 ]; then
      event "heartbeat_tls_error url=$url"
      exit 60
    fi
    event "heartbeat key=$key url=$url cacert=$cacert body=$body"
    if [ "${FAKE_PANEL_STATUS:-200}" != 200 ]; then
      echo '{"detail":"Invalid bot key"}'
      exit 22
    fi
    echo '{}'
    exit 0
    ;;
esac
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
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 90 \\
    -subj "/O=Fake Let's Encrypt/CN=$domain" -addext "subjectAltName=DNS:$domain" \\
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
    for name in ("common.sh", "letsencrypt.sh", *BOT_DEPLOY_FILES):
        if (ROOT / "deploy" / name).exists():  # deploy/bot.sh появляется на шаге 8 плана 25.2
            shutil.copy(ROOT / "deploy" / name, target / "deploy" / name)
    if (ROOT / "bot" / "install-bot.sh").exists():
        (target / "bot").mkdir()
        for name in BOT_FILES:
            if (ROOT / "bot" / name).exists():
                shutil.copy(ROOT / "bot" / name, target / "bot" / name)
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
        "FAKE_BOT_STATE": str(app.parent / "bot-state"),
        "TOLLWIRE_LE_DIR": str(app.parent / "le-hooks"),
        "LE_LIVE_DIR": str(app.parent / "le-live"),
        "TOLLWIRE_LE_HTTP_PORT": str(free_port()),
        "PANEL_LANG": "ru",  # тексты проверяются по-русски; язык меняют тесты в разделе «язык сообщений»
        # Переменные окружения разработчика не должны подменять ответы скриптов.
        "TELEGRAM_BOT_TOKEN": "",
        "BOT_API_KEY": "",
        "TOLLWIRE_REPO_URL": "",
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


# --- бот (этап 25.2) -------------------------------------------------------------------------------------------------


def run_bot_installer(app: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """bot/install-bot.sh («другой сервер»): каталог установки - копия репозитория, как после git clone."""
    environment = {**base_env(app), **env}
    return subprocess.run(
        [str(BASH), "bot/install-bot.sh", *args],
        cwd=app,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def bot_events(app: Path, kind: str | None = None) -> list[str]:
    """События заглушек (выпуск и отзыв ключей, getMe, heartbeat, запуск бота); `kind` - первое слово строки."""
    path = app.parent / "bot-state" / "events.log"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [line for line in lines if kind is None or line.split(" ", 1)[0] == kind]


def issued_keys(app: Path) -> list[str]:
    """Ключи, которые выпустила заглушка, по порядку."""
    return [line.split()[1] for line in bot_events(app, "created")]


def env_bot(app: Path) -> dict[str, str | None]:
    return dotenv_values(app / ".env.bot")


def transcript(app: Path, result: subprocess.CompletedProcess[str]) -> str:
    """Всё, что мог увидеть посторонний: вывод скрипта и аргументы всех вызванных им команд (`ps`, журнал docker)."""
    return "\n".join([result.stdout, result.stderr, *docker_calls(app)])


def positions(calls: list[str], *needles: str) -> list[int]:
    """Индекс первой строки журнала с каждой подстрокой - чтобы проверять порядок шагов."""
    return [next(index for index, line in enumerate(calls) if needle in line) for needle in needles]


def fingerprint_of(pem: Path) -> str:
    """SHA-256 отпечаток сертификата в виде AA:BB:… - так его печатают install.sh и ./panel cert status."""
    text = subprocess.run(
        ["openssl", "x509", "-in", str(pem), "-noout", "-fingerprint", "-sha256"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return text.split("=", 1)[1].strip().upper()


def run_in_terminal(
    app: Path, argv: list[str], answers: tuple[str, ...] = (), *, quiet: float = 0.8, timeout: float = 60, **env: str
) -> tuple[int, str]:
    """Запуск скрипта в псевдотерминале (только POSIX): на вопросы отвечает по очереди, когда вывод затих на `quiet` с.

    Тексты вопросов тесту не нужны: он не ждёт конкретной строки, а отвечает после паузы. Скрытый ввод (`read -s`)
    в вывод не эхом не попадает - по тому, что токена в выводе нет, и видно, что ввод был скрытым."""
    import pty
    import select
    import time

    master, slave = pty.openpty()
    environment = {**base_env(app), "HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port()), **env}
    process = subprocess.Popen(
        [str(BASH), *argv],
        cwd=app,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    output, pending = bytearray(), list(answers)
    last, deadline = time.monotonic(), time.monotonic() + timeout
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                last = time.monotonic()
            elif process.poll() is not None:
                break
            elif pending and time.monotonic() - last > quiet:
                os.write(master, (pending.pop(0) + "\n").encode())
                last = time.monotonic()
            if time.monotonic() > deadline:
                process.kill()
                raise TimeoutError(f"{argv} did not finish; output so far:\n{output.decode(errors='replace')}")
        process.wait(timeout=10)
    finally:
        os.close(master)
    return process.returncode, output.decode(errors="replace").replace("\r\n", "\n")
