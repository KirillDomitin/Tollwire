#!/usr/bin/env bash
# Установщик VPN Control Panel (прод, Docker).
#
#   ./install.sh [--domain panel.example.com] [--cert fullchain.pem --key privkey.pem] [--admin-email EMAIL]
#
# Проверяет окружение, создаёт .env.prod со случайными секретами, готовит TLS-сертификат в certs/,
# запускает панель (docker compose up), ждёт готовности и предлагает создать первого администратора.
#
# Безопасно запускать повторно: существующие .env.prod и сертификат не трогаются никогда. Новый ENCRYPTION_KEY
# сделал бы нечитаемыми все SSH-учётки и VPN-конфиги в базе. Секреты и закрытый ключ на экран не выводятся.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

ENV_FILE=.env.prod
TEMPLATE=.env.prod.example
# Том базы из docker-compose.prod.yml (проект vpn-panel). Если он есть, а .env.prod нет — прошлая установка
# потеряла свои секреты, и новые к этой базе не подойдут.
DB_VOLUME=vpn-panel_pg_data
DEFAULT_DOMAIN=localhost
COMPOSE_FILE=docker-compose.prod.yml
CERT_DIR=certs
CERT_FILE=$CERT_DIR/fullchain.pem
KEY_FILE=$CERT_DIR/privkey.pem
# Самоподписанный сертификат: срок с запасом, чтобы пробная установка не отключилась через год «сама».
SELF_SIGNED_DAYS=825
WARN_EXPIRY_SECONDS=2592000 # 30 дней

tmp=""
tmpdir=""
issue_dir=""
cleanup() {
    [ -z "$tmp" ] || rm -f "$tmp"
    [ -z "$tmpdir" ] || rm -rf "$tmpdir"
    [ -z "$issue_dir" ] || rm -rf "$issue_dir"
}
trap cleanup EXIT

say() { printf '%s\n' "$*"; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Использование: ./install.sh [--domain ДОМЕН_ИЛИ_IP] [--cert ФАЙЛ --key ФАЙЛ] [--admin-email EMAIL] [--no-start]

  --domain   адрес, по которому будет открываться панель (по умолчанию localhost);
             по нему выпускается сертификат. Без параметра в терминале спросит.
  --cert     свой сертификат с цепочкой (PEM), копируется в certs/fullchain.pem;
  --key      его закрытый ключ (PEM, без пароля), копируется в certs/privkey.pem.
             Без них выпускается самоподписанный сертификат (браузер предупредит, что ему не доверяет).
  --admin-email  email первого администратора (иначе спросит в терминале);
  --no-start     только подготовить .env.prod и сертификат, панель не запускать;
  -h, --help эта справка

Переменная INSTALL_WAIT_SECONDS (по умолчанию 300) — сколько ждать готовности панели после запуска.

Существующие .env.prod и certs/*.pem не изменяются: чтобы заменить сертификат, удалите файлы осознанно.
EOF
}

domain=""
domain_given=0
cert_src=""
key_src=""
admin_email=""
start=1
while [ $# -gt 0 ]; do
    case "$1" in
        --domain)
            [ $# -ge 2 ] || die "после --domain нужен адрес."
            domain=$2
            domain_given=1
            shift 2
            ;;
        --cert)
            [ $# -ge 2 ] || die "после --cert нужен путь к файлу."
            cert_src=$2
            shift 2
            ;;
        --key)
            [ $# -ge 2 ] || die "после --key нужен путь к файлу."
            key_src=$2
            shift 2
            ;;
        --admin-email)
            [ $# -ge 2 ] || die "после --admin-email нужен адрес."
            admin_email=$2
            shift 2
            ;;
        --no-start)
            start=0
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "неизвестный параметр: $1"
            ;;
    esac
done

valid_domain='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'
ipv4='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

is_valid_domain() { [[ $1 =~ $valid_domain ]] && [[ $1 != *..* ]]; }

# Проверяет пару «сертификат + закрытый ключ»: оба читаются, ключ подходит к сертификату, срок не вышел.
check_pair() {
    local cert=$1 key=$2 cert_pub key_pub
    openssl x509 -in "$cert" -noout > /dev/null 2>&1 || die "$cert — не сертификат в формате PEM."
    # -passin pass: — ключ с паролем не должен заставить openssl ждать ввода: он просто не прочитается.
    openssl pkey -in "$key" -passin pass: -noout > /dev/null 2>&1 \
        || die "$key — не закрытый ключ в формате PEM (ключи с паролем не поддерживаются)."
    cert_pub=$(openssl x509 -in "$cert" -noout -pubkey)
    key_pub=$(openssl pkey -in "$key" -passin pass: -pubout)
    [ "$cert_pub" = "$key_pub" ] || die "ключ $key не подходит к сертификату $cert."
    openssl x509 -in "$cert" -noout -checkend 0 > /dev/null || die "сертификат $cert истёк. Положите действующий или удалите оба файла в $CERT_DIR/, чтобы выпустить новый."
}

# --- проверка окружения ---------------------------------------------------------------------------------------------

command -v docker > /dev/null 2>&1 || die "Docker не найден. Установите Docker Engine: https://docs.docker.com/engine/install/"
docker info > /dev/null 2>&1 \
    || die "нет связи с Docker: демон не запущен или нет прав. Запустите установщик от root или добавьте пользователя в группу docker."
docker compose version > /dev/null 2>&1 \
    || die "не найден плагин Docker Compose (команда 'docker compose'). Установите docker-compose-plugin."
command -v openssl > /dev/null 2>&1 || die "openssl не найден: он нужен для генерации секретов и сертификата."
[ -f "$TEMPLATE" ] || die "не найден шаблон $TEMPLATE: запускайте установщик из корня репозитория."
[ -f "$COMPOSE_FILE" ] || die "не найден $COMPOSE_FILE: запускайте установщик из корня репозитория."
# Адрес уходит аргументом команды, но проверяется строго: опечатку лучше поймать до записи секретов.
if [ -n "$admin_email" ] && ! [[ $admin_email =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    die "некорректный email администратора: $admin_email"
fi

# --- сертификат: что делать, решаем ДО записи чего-либо ----------------------------------------------------------------
# Ошибка в сертификате не должна оставить после себя .env.prod с уже сгенерированными секретами.

if [ -n "$cert_src" ] || [ -n "$key_src" ]; then
    { [ -n "$cert_src" ] && [ -n "$key_src" ]; } || die "--cert и --key указываются вместе."
    [ -f "$cert_src" ] || die "файл сертификата не найден: $cert_src"
    [ -f "$key_src" ] || die "файл ключа не найден: $key_src"
    if [ -e "$CERT_FILE" ] || [ -e "$KEY_FILE" ]; then
        die "в $CERT_DIR/ уже есть сертификат — свой не заменит его молча. Удалите $CERT_FILE и $KEY_FILE осознанно и запустите снова."
    fi
    check_pair "$cert_src" "$key_src"
    cert_mode=own
elif [ -e "$CERT_FILE" ] && [ -e "$KEY_FILE" ]; then
    check_pair "$CERT_FILE" "$KEY_FILE"
    cert_mode=existing
elif [ -e "$CERT_FILE" ] || [ -e "$KEY_FILE" ]; then
    [ -e "$CERT_FILE" ] && missing=$KEY_FILE || missing=$CERT_FILE
    die "в $CERT_DIR/ нет файла $missing, хотя второй из пары есть. Положите пару целиком или удалите оставшийся файл."
else
    cert_mode=generate
fi

# --- .env.prod -----------------------------------------------------------------------------------------------------

env_get() {
    # Значение переменной из готового .env.prod; комментарий после значения и кавычки отбрасываются.
    local value
    value=$(grep -m1 "^$1=" "$ENV_FILE" 2> /dev/null | cut -d= -f2- || true)
    value=${value%%#*}
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value=${value#[\"\']}
    value=${value%[\"\']}
    printf '%s' "$value"
}

env_domain() {
    # PANEL_DOMAIN из готового .env.prod, если задан и корректен.
    local value
    value=$(env_get PANEL_DOMAIN)
    if [ -n "$value" ] && is_valid_domain "$value"; then
        printf '%s' "$value"
    fi
}

if [ -e "$ENV_FILE" ]; then
    say "Файл $ENV_FILE уже существует — оставляю без изменений."
    if [ -n "$(find "$ENV_FILE" -maxdepth 0 -perm /077 2> /dev/null)" ]; then
        say "Внимание: $ENV_FILE доступен не только владельцу. Закройте доступ: chmod 600 $ENV_FILE"
    fi
    from_env=$(env_domain)
    if [ -n "$from_env" ]; then
        if [ "$domain_given" -eq 1 ] && [ "$domain" != "$from_env" ]; then
            say "Адрес панели берётся из $ENV_FILE (PANEL_DOMAIN=$from_env); параметр --domain игнорируется."
        fi
        domain=$from_env
    elif [ "$domain_given" -eq 1 ]; then
        is_valid_domain "$domain" || die "некорректный домен. Допустимы буквы, цифры, точки и дефисы (например, panel.example.com или 203.0.113.5)."
    else
        domain=$DEFAULT_DOMAIN
    fi
    env_created=0
else
    if [ -n "$(docker volume ls -q --filter "name=^${DB_VOLUME}\$" 2> /dev/null)" ]; then
        die "найден том с данными прошлой установки ($DB_VOLUME), а файла $ENV_FILE нет. Новые пароль БД и ключ шифрования к ним не подойдут. Восстановите $ENV_FILE из копии. Если данные не нужны — удалите том осознанно: docker volume rm $DB_VOLUME"
    fi

    # Домен попадает в сертификат и в .env.prod, поэтому проверяется строго: только буквы, цифры, точки и дефисы.
    if [ "$domain_given" -eq 0 ]; then
        domain=$DEFAULT_DOMAIN
        if [ -t 0 ] && [ -t 1 ]; then
            read -r -p "Домен или IP панели [$DEFAULT_DOMAIN]: " answer || answer=""
            domain=${answer:-$DEFAULT_DOMAIN}
        fi
    fi
    is_valid_domain "$domain" || die "некорректный домен. Допустимы буквы, цифры, точки и дефисы (например, panel.example.com или 203.0.113.5)."

    postgres_password=$(openssl rand -hex 32)
    jwt_secret=$(openssl rand -hex 32)
    # Ключ Fernet — 32 случайных байта в base64url (44 знака).
    encryption_key=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')

    tmp=$(umask 077 && mktemp "${ENV_FILE}.XXXXXX")

    # Шаблон копируется построчно, подменяются только секреты и домен: остальные настройки и пояснения остаются.
    # Секреты не передаются аргументами команд (их видно в списке процессов).
    replaced=""
    while IFS= read -r line || [ -n "$line" ]; do
        case $line in
            POSTGRES_PASSWORD=*) printf 'POSTGRES_PASSWORD=%s\n' "$postgres_password"; replaced+=" POSTGRES_PASSWORD" ;;
            JWT_SECRET_KEY=*) printf 'JWT_SECRET_KEY=%s\n' "$jwt_secret"; replaced+=" JWT_SECRET_KEY" ;;
            ENCRYPTION_KEY=*) printf 'ENCRYPTION_KEY=%s\n' "$encryption_key"; replaced+=" ENCRYPTION_KEY" ;;
            PANEL_DOMAIN=*) printf 'PANEL_DOMAIN=%s\n' "$domain"; replaced+=" PANEL_DOMAIN" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < "$TEMPLATE" > "$tmp"

    # Шаблон мог разойтись с установщиком: лучше отказ, чем файл, где в ключе шифрования осталась заглушка.
    for key in POSTGRES_PASSWORD JWT_SECRET_KEY ENCRYPTION_KEY PANEL_DOMAIN; do
        case " $replaced " in
            *" $key "*) ;;
            *) die "в $TEMPLATE нет строки $key=… — установщик и шаблон разошлись." ;;
        esac
    done
    if grep -q 'change_me' "$tmp"; then
        die "в $TEMPLATE остались заглушки change_me, которые установщик не умеет заполнять."
    fi

    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
    tmp=""
    env_created=1
fi

# --- TLS-сертификат ------------------------------------------------------------------------------------------------

cert_expiry() { openssl x509 -in "$1" -noout -enddate | cut -d= -f2; }

# Положить пару в certs/ целиком или никак: сначала во временный каталог рядом, затем переименованием.
install_pair() {
    local cert=$1 key=$2
    mkdir -p "$CERT_DIR"
    tmpdir=$(umask 077 && mktemp -d "$CERT_DIR/.install.XXXXXX")
    cp "$cert" "$tmpdir/fullchain.pem"
    cp "$key" "$tmpdir/privkey.pem"
    chmod 644 "$tmpdir/fullchain.pem"
    chmod 600 "$tmpdir/privkey.pem"
    mv "$tmpdir/fullchain.pem" "$CERT_FILE"
    mv "$tmpdir/privkey.pem" "$KEY_FILE"
    rm -rf "$tmpdir"
    tmpdir=""
}

issue_self_signed() {
    local san
    if [[ $domain =~ $ipv4 ]]; then
        san="IP:$domain"
    elif [ "$domain" = localhost ]; then
        san="DNS:localhost,IP:127.0.0.1"
    else
        san="DNS:$domain"
    fi
    mkdir -p "$CERT_DIR"
    issue_dir=$(umask 077 && mktemp -d "$CERT_DIR/.issue.XXXXXX")
    # Эллиптический ключ: быстрый и маленький, поддерживается всеми современными браузерами и nginx.
    # MSYS_NO_PATHCONV: в Git Bash на Windows «/CN=…» иначе превращается в путь к файлу (на Linux ни на что не влияет).
    MSYS_NO_PATHCONV=1 openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
        -days "$SELF_SIGNED_DAYS" -subj "/CN=$domain" -addext "subjectAltName=$san" \
        -keyout "$issue_dir/privkey.pem" -out "$issue_dir/fullchain.pem" > /dev/null 2>&1 \
        || die "не удалось выпустить самоподписанный сертификат для «$domain»."
    install_pair "$issue_dir/fullchain.pem" "$issue_dir/privkey.pem"
    rm -rf "$issue_dir"
    issue_dir=""
}

case $cert_mode in
    generate)
        issue_self_signed
        fingerprint=$(openssl x509 -in "$CERT_FILE" -noout -fingerprint -sha256 | cut -d= -f2)
        say "Выпущен самоподписанный сертификат для $domain (до: $(cert_expiry "$CERT_FILE"))."
        say "Браузер предупредит, что не доверяет ему, — для пробного запуска это нормально."
        say "Отпечаток SHA-256 (сверьте с тем, что покажет браузер): $fingerprint"
        say "Для боевого домена используйте свой сертификат: ./install.sh --cert fullchain.pem --key privkey.pem"
        say "(или положите файлы в $CERT_DIR/ вручную: $CERT_FILE и $KEY_FILE)."
        ;;
    own)
        install_pair "$cert_src" "$key_src"
        say "Ваш сертификат установлен в $CERT_DIR/ (до: $(cert_expiry "$CERT_FILE"))."
        ;;
    existing)
        say "Сертификат в $CERT_DIR/ найден — оставляю без изменений (до: $(cert_expiry "$CERT_FILE"))."
        ;;
esac

if [ "$cert_mode" != generate ]; then
    openssl x509 -in "$CERT_FILE" -noout -checkend "$WARN_EXPIRY_SECONDS" > /dev/null \
        || say "Внимание: сертификат скоро истекает ($(cert_expiry "$CERT_FILE")). Обновите файлы в $CERT_DIR/ и перезапустите nginx."
    if [[ $domain =~ $ipv4 ]]; then
        covers=$(openssl x509 -in "$CERT_FILE" -noout -checkip "$domain" 2> /dev/null || true)
    else
        covers=$(openssl x509 -in "$CERT_FILE" -noout -checkhost "$domain" 2> /dev/null || true)
    fi
    case $covers in
        *"does match"*) ;;
        *) say "Внимание: сертификат не покрывает адрес $domain — браузер покажет ошибку. Проверьте домен сертификата." ;;
    esac
fi

if [ "$env_created" -eq 1 ]; then
    say ""
    say "Создан $ENV_FILE (доступ только владельцу): случайные пароль БД, JWT-секрет и ключ шифрования."
    say ""
    say "ВАЖНО: сохраните копию $ENV_FILE отдельно от этого сервера (менеджер паролей, сейф)."
    say "ENCRYPTION_KEY нельзя ни потерять, ни сменить: им зашифрованы SSH-учётки и VPN-конфиги в базе."
fi

# --- запуск --------------------------------------------------------------------------------------------------------

compose=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
compose_text="docker compose --env-file $ENV_FILE -f $COMPOSE_FILE"

if [ "$start" -eq 0 ]; then
    say ""
    say "Запуск пропущен (--no-start). Когда будете готовы: $compose_text up -d --build"
    exit 0
fi

wait_seconds=${INSTALL_WAIT_SECONDS:-300}
[[ $wait_seconds =~ ^[1-9][0-9]*$ ]] || die "INSTALL_WAIT_SECONDS=«$wait_seconds» — нужно целое число секунд больше нуля."

# Порт: переменная окружения (compose читает и её, она важнее файла), затем .env.prod, затем умолчание.
port_setting() {
    local name=$1 value
    value=${!name:-}
    [ -n "$value" ] || value=$(env_get "$name")
    value=${value:-$2}
    if ! [[ $value =~ ^[0-9]{1,5}$ ]] || [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
        die "$name=«$value» — это не номер порта (1–65535)."
    fi
    printf '%s' "$value"
}
http_port=$(port_setting HTTP_PORT 80)
https_port=$(port_setting HTTPS_PORT 443)

# Занятый порт compose заметит, лишь когда nginx уже не сможет стартовать, — лучше сказать об этом сразу.
# Если панель уже работает, порты заняты нами самими (повторный запуск) — тогда не проверяем.
port_busy() { (exec 3<> "/dev/tcp/127.0.0.1/$1") 2> /dev/null; }
if [ -z "$("${compose[@]}" ps -q nginx 2> /dev/null)" ]; then
    for port in "$http_port" "$https_port"; do
        if port_busy "$port"; then
            die "порт $port уже занят другой программой. Освободите его или задайте другой в $ENV_FILE (HTTP_PORT, HTTPS_PORT) и запустите установщик снова."
        fi
    done
fi

say ""
say "Запускаю панель. Первая сборка образов занимает несколько минут…"
"${compose[@]}" up -d --build || die "не удалось запустить панель. Что случилось: $compose_text logs --tail 100"

# Готовность: контейнер api здоров (проверка внутри compose) и nginx отдаёт /health по HTTPS (-k: сертификат
# может быть самоподписанным, цепочку проверить нечем; здесь важно, что TLS и проксирование работают).
api_status=""
probe_https() {
    command -v curl > /dev/null 2>&1 || return 0
    curl -fsSk --max-time 5 "https://127.0.0.1:$https_port/health" > /dev/null 2>&1
}
wait_ready() {
    local deadline=$((SECONDS + wait_seconds)) id
    while :; do
        id=$("${compose[@]}" ps -q api 2> /dev/null | head -n 1 || true)
        api_status=""
        [ -z "$id" ] || api_status=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2> /dev/null || true)
        if [ "$api_status" = healthy ] && probe_https; then
            return 0
        fi
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep 2
    done
}

say "Жду готовности панели (до $wait_seconds с)…"
if ! wait_ready; then
    "${compose[@]}" logs --tail 30 api nginx 2>&1 | sed 's/^/  | /' >&2 || true
    if [ "$api_status" != healthy ]; then
        die "контейнер api не стал готовым за $wait_seconds с (статус: ${api_status:-нет}). Последние строки логов выше; полные: $compose_text logs"
    fi
    die "api готов, но https://127.0.0.1:$https_port/health не отвечает по HTTPS: смотрите nginx и сертификат. Полные логи: $compose_text logs"
fi

# --- первый администратор ------------------------------------------------------------------------------------------

admin_cmd=("${compose[@]}" run --rm api python -m scripts.create_admin --only-if-empty)
[ -z "$admin_email" ] || admin_cmd+=("$admin_email")
admin_text=$(printf '%q ' "${admin_cmd[@]}")
admin_text=${admin_text% }

say ""
if [ -t 0 ] && [ -t 1 ]; then
    say "Создаём первого администратора (если админов ещё нет):"
    "${admin_cmd[@]}" || die "не удалось создать администратора. Повторите: $admin_text"
else
    say "Администратор не создан: для ввода пароля нужен терминал. Создайте его командой:"
    say "  $admin_text"
fi

# --- итог ----------------------------------------------------------------------------------------------------------

url="https://$domain"
[ "$https_port" -eq 443 ] || url="$url:$https_port"
say ""
say "Панель запущена: $url"
if [ "$cert_mode" = generate ]; then
    say "Сертификат самоподписанный: браузер предупредит, что не доверяет ему, — для пробного запуска это нормально."
fi
say "Логи:      $compose_text logs -f"
say "Остановка: $compose_text down"
if [ "$env_created" -eq 1 ]; then
    say ""
    say "Не забудьте сохранить копию $ENV_FILE отдельно от этого сервера: без ENCRYPTION_KEY данные в базе не прочитать."
fi
