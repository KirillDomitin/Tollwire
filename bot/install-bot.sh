#!/usr/bin/env bash
# Установка Telegram-бота на отдельном сервере (панель стоит на другом).
#
#   git clone <адрес репозитория> tollwire && cd tollwire
#   ./bot/install-bot.sh --panel-url https://panel.example.com [--panel-fingerprint SHA256]
#
# Команду целиком, с адресом панели и отпечатком сертификата, печатает установщик панели (./install.sh --bot remote,
# ./panel bot key --remote). Ключ бота и токен Telegram скрипт спрашивает скрытым вводом либо берёт из переменных
# BOT_API_KEY и TELEGRAM_BOT_TOKEN; в аргументы команд они не попадают (curl читает их из stdin), на экран не
# выводятся, лежат только в bot/.env с правами 0600.
#
# Порядок: токен проверяется у Telegram (getMe), ключ — у панели (heartbeat); если сертификат панели самоподписанный,
# он сохраняется в bot/certs/panel-ca.pem только после сверки отпечатка. Пока проверки не прошли, ничего не пишется.
# --reconfigure меняет только ключ (после ./panel bot key --remote): токен и адрес берутся из bot/.env.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BOT_DIR=bot
BOT_ENV=$BOT_DIR/.env
BOT_COMPOSE=$BOT_DIR/docker-compose.yml
CA_DIR=$BOT_DIR/certs
CA_FILE=$CA_DIR/panel-ca.pem
CA_IN_CONTAINER=/certs/panel-ca.pem

# Язык сообщений, t, say, die и env_set — общее с install.sh и ./panel; проверки бота — в deploy/bot.sh.
# shellcheck source=deploy/common.sh
source deploy/common.sh
# shellcheck source=deploy/bot.sh
source deploy/bot.sh

tmp=""
cleanup() {
    [ -z "$tmp" ] || rm -f "$tmp"
}
trap cleanup EXIT

msg_ru() {
    case $1 in
        unknown_param) printf '%s' "неизвестный параметр: %s" ;;
        need_value) printf '%s' "после %s нужно значение." ;;
        need_panel_url) printf '%s' "укажите адрес панели: --panel-url https://panel.example.com" ;;
        need_https) printf '%s' "адрес панели должен быть вида https://домен[:порт], без пути и логина: бот не отправляет ключ по открытому http." ;;
        no_docker) printf '%s' "Docker не найден. Установите Docker Engine: https://docs.docker.com/engine/install/" ;;
        docker_unreachable) printf '%s' "нет связи с Docker: демон не запущен или нет прав. Запустите скрипт от root или добавьте пользователя в группу docker." ;;
        no_compose) printf '%s' "не найден плагин Docker Compose (команда 'docker compose'). Установите docker-compose-plugin." ;;
        no_curl) printf '%s' "curl не найден: он нужен для проверки токена и панели." ;;
        need_secret) printf '%s' "не задан %s: передайте его переменной окружения или запустите скрипт в терминале." ;;
        key_prompt) printf '%s' "Ключ бота (из ./panel bot key --remote на сервере панели, ввод скрыт): " ;;
        token_prompt) printf '%s' "Токен бота от @BotFather (ввод скрыт): " ;;
        bad_key) printf '%s' "это не похоже на ключ бота (ожидается twb_… и ещё 43 символа)." ;;
        bad_token) printf '%s' "это не похоже на токен бота (ожидается вид 123456789:AAF… от @BotFather)." ;;
        telegram_failed) printf '%s' "Telegram не принял токен или недоступен с этого сервера. Ничего не записано." ;;
        key_rejected) printf '%s' "панель не приняла ключ: он неверный или отозван. Выпустите новый на сервере панели: ./panel bot key --remote" ;;
        panel_unreachable) printf '%s' "панель %s не отвечает: проверьте адрес, порт и сеть. Ничего не записано." ;;
        need_fingerprint) printf '%s' "сертификат панели не доверенный (самоподписанный?). Сверьте его отпечаток с ./panel cert status на сервере панели и запустите снова с --panel-fingerprint ОТПЕЧАТОК." ;;
        fingerprint_mismatch) printf '%s' "отпечаток сертификата панели (%s) не совпал с заданным: возможна подмена сервера. Ничего не записано." ;;
        fingerprint_show) printf '%s' "Сертификат панели не доверенный. Его отпечаток SHA-256: %s" ;;
        fingerprint_ask) printf '%s' "Сверьте его с ./panel cert status на сервере панели. Доверять этому сертификату? [y/N]: " ;;
        fingerprint_declined) printf '%s' "отказ: сертификату панели не доверяем, ничего не записано." ;;
        cert_fetch_failed) printf '%s' "не удалось получить сертификат панели с %s." ;;
        no_openssl) printf '%s' "openssl не найден: он нужен, чтобы получить и сверить сертификат панели." ;;
        env_written) printf '%s' "Настройки бота записаны в %s (доступ только владельцу)." ;;
        starting) printf '%s' "Запускаю бота. Первая сборка образа занимает несколько минут…" ;;
        start_failed) printf '%s' "не удалось запустить бота. Что случилось: docker compose -f bot/docker-compose.yml logs bot" ;;
        waiting) printf '%s' "Жду, пока бот станет готовым (до %s с)…" ;;
        not_healthy) printf '%s' "бот не стал готовым за %s с (статус: %s). Что он пишет: docker compose -f bot/docker-compose.yml logs bot" ;;
        running) printf '%s' "Бот @%s запущен и на связи с панелью." ;;
        no_install) printf '%s' "бот ещё не установлен (нет bot/.env): запустите установку без --reconfigure." ;;
        url_differs) printf '%s' "адрес панели в bot/.env (%s) отличается от --panel-url: для смены адреса запустите установку без --reconfigure." ;;
        cert_changed) printf '%s' "сертификат панели %s больше не совпадает с сохранённым (bot/certs/panel-ca.pem). Если панель сменила сертификат, запустите установку заново без --reconfigure." ;;
        reconfigured) printf '%s' "Ключ бота заменён. Старый ключ отзовите на сервере панели: ./panel bot revoke" ;;
        none) printf '%s' "нет" ;;
    esac
}

msg_en() {
    case $1 in
        unknown_param) printf '%s' "unknown option: %s" ;;
        need_value) printf '%s' "%s needs a value." ;;
        need_panel_url) printf '%s' "give the panel address: --panel-url https://panel.example.com" ;;
        need_https) printf '%s' "the panel address must look like https://domain[:port], without a path or a login: the bot does not send its key over plain http." ;;
        no_docker) printf '%s' "Docker not found. Install Docker Engine: https://docs.docker.com/engine/install/" ;;
        docker_unreachable) printf '%s' "cannot talk to Docker: the daemon is not running or you lack permission. Run the script as root or add your user to the docker group." ;;
        no_compose) printf '%s' "the Docker Compose plugin ('docker compose') was not found. Install docker-compose-plugin." ;;
        no_curl) printf '%s' "curl not found: it is needed to check the token and the panel." ;;
        need_secret) printf '%s' "%s is not set: pass it as an environment variable or run the script in a terminal." ;;
        key_prompt) printf '%s' "Bot key (from ./panel bot key --remote on the panel server, input hidden): " ;;
        token_prompt) printf '%s' "Bot token from @BotFather (input hidden): " ;;
        bad_key) printf '%s' "this does not look like a bot key (expected twb_... and 43 more characters)." ;;
        bad_token) printf '%s' "this does not look like a bot token (expected 123456789:AAF... from @BotFather)." ;;
        telegram_failed) printf '%s' "Telegram rejected the token or is unreachable from this server. Nothing was written." ;;
        key_rejected) printf '%s' "the panel rejected the key: it is wrong or revoked. Issue a new one on the panel server: ./panel bot key --remote" ;;
        panel_unreachable) printf '%s' "the panel %s does not answer: check the address, the port and the network. Nothing was written." ;;
        need_fingerprint) printf '%s' "the panel certificate is not trusted (self-signed?). Compare its fingerprint with ./panel cert status on the panel server and run again with --panel-fingerprint FINGERPRINT." ;;
        fingerprint_mismatch) printf '%s' "the panel certificate fingerprint (%s) does not match the given one: the server may be spoofed. Nothing was written." ;;
        fingerprint_show) printf '%s' "The panel certificate is not trusted. Its SHA-256 fingerprint: %s" ;;
        fingerprint_ask) printf '%s' "Compare it with ./panel cert status on the panel server. Trust this certificate? [y/N]: " ;;
        fingerprint_declined) printf '%s' "declined: the panel certificate is not trusted, nothing was written." ;;
        cert_fetch_failed) printf '%s' "could not fetch the panel certificate from %s." ;;
        no_openssl) printf '%s' "openssl not found: it is needed to fetch and check the panel certificate." ;;
        env_written) printf '%s' "Bot settings are saved in %s (owner-only access)." ;;
        starting) printf '%s' "Starting the bot. The first image build takes a few minutes..." ;;
        start_failed) printf '%s' "could not start the bot. What happened: docker compose -f bot/docker-compose.yml logs bot" ;;
        waiting) printf '%s' "Waiting for the bot to become ready (up to %s s)..." ;;
        not_healthy) printf '%s' "the bot did not become ready within %s s (status: %s). What it says: docker compose -f bot/docker-compose.yml logs bot" ;;
        running) printf '%s' "The bot @%s is running and talks to the panel." ;;
        no_install) printf '%s' "the bot is not installed yet (no bot/.env): run the installation without --reconfigure." ;;
        url_differs) printf '%s' "the panel address in bot/.env (%s) differs from --panel-url: to change the address run the installation without --reconfigure." ;;
        cert_changed) printf '%s' "the certificate of the panel %s no longer matches the saved one (bot/certs/panel-ca.pem). If the panel changed its certificate, run the installation again without --reconfigure." ;;
        reconfigured) printf '%s' "The bot key is replaced. Revoke the old key on the panel server: ./panel bot revoke" ;;
        none) printf '%s' "none" ;;
    esac
}

usage() {
    if [ "$lang" = ru ]; then
        cat <<'EOF'
Использование: ./bot/install-bot.sh --panel-url https://ПАНЕЛЬ[:порт] [--panel-fingerprint ОТПЕЧАТОК] [--reconfigure]

  --panel-url          адрес панели, только https (без пути и логина);
  --panel-fingerprint  SHA-256 отпечаток сертификата панели (./panel cert status на сервере панели): нужен, если
                       сертификат самоподписанный; без него в терминале спросит подтверждение, без терминала откажет;
  --reconfigure        заменить только ключ бота: токен и адрес берутся из bot/.env;
  -h, --help           эта справка

Ключ бота и токен Telegram: переменные BOT_API_KEY и TELEGRAM_BOT_TOKEN либо скрытый ввод в терминале.
INSTALL_WAIT_SECONDS (по умолчанию 120) — сколько ждать готовности бота. Язык: PANEL_LANG=ru|en.
EOF
    else
        cat <<'EOF'
Usage: ./bot/install-bot.sh --panel-url https://PANEL[:port] [--panel-fingerprint FINGERPRINT] [--reconfigure]

  --panel-url          the panel address, https only (no path or login);
  --panel-fingerprint  SHA-256 fingerprint of the panel certificate (./panel cert status on the panel server): needed
                       when the certificate is self-signed; without it a terminal asks for confirmation, no terminal
                       refuses;
  --reconfigure        replace only the bot key: the token and address come from bot/.env;
  -h, --help           this help

The bot key and the Telegram token: the BOT_API_KEY and TELEGRAM_BOT_TOKEN variables or hidden input in a terminal.
INSTALL_WAIT_SECONDS (120 by default) is how long to wait for the bot to become ready. Language: PANEL_LANG=ru|en.
EOF
    fi
}

panel_url=""
fingerprint=""
reconfigure=0
while [ $# -gt 0 ]; do
    case "$1" in
        --panel-url)
            [ $# -ge 2 ] || die "$(t need_value --panel-url)"
            panel_url=$2
            shift 2
            ;;
        --panel-fingerprint)
            [ $# -ge 2 ] || die "$(t need_value --panel-fingerprint)"
            fingerprint=$2
            shift 2
            ;;
        --reconfigure)
            reconfigure=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "$(t unknown_param "$1")"
            ;;
    esac
done

# --- проверки до всего остального -----------------------------------------------------------------------------------

existing_url=""
if [ "$reconfigure" -eq 1 ]; then
    [ -f "$BOT_ENV" ] || die "$(t no_install)"
    existing_url=$(bot_env_get "$BOT_ENV" PANEL_URL)
fi
[ -n "$panel_url" ] || panel_url=$existing_url
[ -n "$panel_url" ] || die "$(t need_panel_url)"
panel_url=${panel_url%/}
[ "$reconfigure" -eq 0 ] || [ "$panel_url" = "$existing_url" ] || die "$(t url_differs "$existing_url")"
# Адрес попадает в конфиг curl (`url = "…"`): кавычка, пробел, обратная косая и логин здесь недопустимы.
[[ $panel_url =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]] || die "$(t need_https)"

command -v docker > /dev/null 2>&1 || die "$(t no_docker)"
docker info > /dev/null 2>&1 || die "$(t docker_unreachable)"
docker compose version > /dev/null 2>&1 || die "$(t no_compose)"
bot_compose_ok || die "$(t bot_old_compose "${bot_compose_version:-?}")"
command -v curl > /dev/null 2>&1 || die "$(t no_curl)"

# Секреты: переменная окружения, а в терминале — скрытый ввод. Без терминала недостающий называется по имени.
read_secret() {
    local value=${!1:-}
    if [ -z "$value" ] && bot_terminal; then
        read -r -s -p "$2" value || value=""
        echo
    fi
    [ -n "$value" ] || die "$(t need_secret "$1")"
    secret=$value
}

secret=""
read_secret BOT_API_KEY "$(t key_prompt)"
api_key=$secret
bot_key_ok "$api_key" || die "$(t bad_key)"
if [ "$reconfigure" -eq 1 ]; then
    token=$(bot_env_get "$BOT_ENV" TELEGRAM_BOT_TOKEN)
else
    read_secret TELEGRAM_BOT_TOKEN "$(t token_prompt)"
    token=$secret
fi
unset BOT_API_KEY TELEGRAM_BOT_TOKEN # дальше значения нужны только в памяти скрипта: дочерним процессам их не передаём
bot_token_ok "$token" || die "$(t bad_token)"

# --- Telegram: токен -------------------------------------------------------------------------------------------------

username=$(bot_get_me "$token") || die "$(t telegram_failed)"

# --- панель: ключ и сертификат ---------------------------------------------------------------------------------------

cacert=""
if [ "$reconfigure" -eq 1 ] && [ -n "$(bot_env_get "$BOT_ENV" PANEL_CA_FILE)" ] && [ -f "$CA_FILE" ]; then
    cacert=$CA_FILE
fi

# Heartbeat с этим ключом: панель отвечает 200, только если ключ действует. Ключ идёт в конфиг curl через stdin.
panel_heartbeat() {
    local args=()
    [ -z "$cacert" ] || args=(--cacert "$cacert")
    {
        printf 'url = "%s/api/v1/bot/heartbeat"\n' "$panel_url"
        printf 'header = "X-Bot-Key: %s"\n' "$api_key"
        printf 'header = "Content-Type: application/json"\n'
        printf 'data = "{\\"bot_username\\":\\"%s\\",\\"version\\":\\"install\\"}"\n' "$username"
    } | curl -q -fsS --max-time 15 ${args[@]+"${args[@]}"} -K - > /dev/null 2>&1
}

normalize_fingerprint() { printf '%s' "$1" | tr -d ': \t\r\n' | tr 'a-f' 'A-F'; }

# Сертификат, который панель предъявляет по адресу (первый в цепочке — её собственный).
fetch_panel_certificate() {
    local host=${panel_url#https://} port=443 servername=() guard=()
    case $host in *:*)
        port=${host##*:}
        host=${host%%:*}
        ;;
    esac
    [[ $host =~ ^[0-9.]+$ ]] || servername=(-servername "$host")
    command -v timeout > /dev/null 2>&1 && guard=(timeout 20)
    printf '' | ${guard[@]+"${guard[@]}"} openssl s_client -connect "$host:$port" ${servername[@]+"${servername[@]}"} -showcerts 2> /dev/null \
        | openssl x509 -outform PEM 2> /dev/null
}

# Панель предъявила сертификат, которому система не верит (обычно самоподписанный). Доверяем ему только после сверки
# отпечатка: заданного --panel-fingerprint либо подтверждённого человеком в терминале.
trust_panel_certificate() {
    command -v openssl > /dev/null 2>&1 || die "$(t no_openssl)"
    local pem actual
    pem=$(fetch_panel_certificate) || pem=""
    case $pem in *"BEGIN CERTIFICATE"*) ;; *) die "$(t cert_fetch_failed "$panel_url")" ;; esac
    actual=$(printf '%s\n' "$pem" | openssl x509 -noout -fingerprint -sha256 | cut -d= -f2)
    if [ -n "$fingerprint" ]; then
        [ "$(normalize_fingerprint "$actual")" = "$(normalize_fingerprint "$fingerprint")" ] \
            || die "$(t fingerprint_mismatch "$actual")"
    elif bot_terminal; then
        say "$(t fingerprint_show "$actual")"
        local answer=""
        read -r -p "$(t fingerprint_ask)" answer || answer=""
        case $answer in y | Y | yes | YES) ;; *) die "$(t fingerprint_declined)" ;; esac
    else
        die "$(t need_fingerprint)"
    fi
    mkdir -p "$CA_DIR"
    tmp=$(mktemp "$CA_DIR/.ca.XXXXXX")
    printf '%s\n' "$pem" > "$tmp"
    chmod 644 "$tmp"
    mv "$tmp" "$CA_FILE"
    tmp=""
    cacert=$CA_FILE
}

check_panel() {
    local rc=0
    panel_heartbeat || rc=$?
    case $rc in
        0) return 0 ;;
        35 | 51 | 58 | 60 | 77 | 83)
            # Не доверяем сертификату панели: без сверки отпечатка ключ дальше не отправляется.
            [ -z "$cacert" ] || die "$(t cert_changed "$panel_url")"
            trust_panel_certificate
            rc=0
            panel_heartbeat || rc=$?
            ;;
    esac
    case $rc in
        0) return 0 ;;
        22) die "$(t key_rejected)" ;;
        *) die "$(t panel_unreachable "$panel_url")" ;;
    esac
}

check_panel

# --- запись настроек и запуск ----------------------------------------------------------------------------------------

mkdir -p "$CA_DIR" # каталог создаём сами: иначе Docker создал бы его от root (bind-mount), и сюда нельзя было бы писать
if [ "$reconfigure" -eq 1 ]; then
    (ENV_FILE=$BOT_ENV && env_set BOT_API_KEY "$api_key")
    # Панель стала самоподписанной уже после установки: бот должен узнать про свежесохранённый сертификат.
    if [ -n "$cacert" ] && [ -z "$(bot_env_get "$BOT_ENV" PANEL_CA_FILE)" ]; then
        (ENV_FILE=$BOT_ENV && env_set PANEL_CA_FILE "$CA_IN_CONTAINER")
    fi
else
    tmp=$(umask 077 && mktemp "$BOT_ENV.XXXXXX")
    {
        printf '# Создано bot/install-bot.sh. Не коммитить: здесь токен Telegram и ключ бота.\n'
        printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token"
        printf 'BOT_API_KEY=%s\n' "$api_key"
        printf 'PANEL_URL=%s\n' "$panel_url"
        [ -z "$cacert" ] || printf 'PANEL_CA_FILE=%s\n' "$CA_IN_CONTAINER"
        printf 'LOG_LEVEL=INFO\n'
    } > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$BOT_ENV"
    tmp=""
fi
say "$(t env_written "$BOT_ENV")"

say "$(t starting)"
if [ "$reconfigure" -eq 1 ]; then
    docker compose -f "$BOT_COMPOSE" up -d --force-recreate || die "$(t start_failed)"
else
    docker compose -f "$BOT_COMPOSE" up -d --build || die "$(t start_failed)"
fi

wait_seconds=${INSTALL_WAIT_SECONDS:-120}
[[ $wait_seconds =~ ^[1-9][0-9]*$ ]] || wait_seconds=120
say "$(t waiting "$wait_seconds")"
deadline=$((SECONDS + wait_seconds))
status=""
while :; do
    id=$(docker compose -f "$BOT_COMPOSE" ps -q bot 2> /dev/null | head -n 1 || true)
    status=""
    [ -z "$id" ] || status=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2> /dev/null || true)
    [ "$status" != healthy ] || break
    [ "$SECONDS" -lt "$deadline" ] || die "$(t not_healthy "$wait_seconds" "${status:-$(t none)}")"
    sleep 2
done

if [ "$reconfigure" -eq 1 ]; then
    say "$(t reconfigured)"
else
    say "$(t running "$username")"
fi
