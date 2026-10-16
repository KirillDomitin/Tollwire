#!/usr/bin/env bash
# Установщик Tollwire (прод, Docker).
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

# Язык сообщений, t, say и die — общее с ./panel.
# shellcheck source=deploy/common.sh
source deploy/common.sh

# Сообщения: ключ → строка для printf (подстановки %s — по порядку, одинаковому в обоих языках).
msg_ru() {
    case $1 in
        need_domain) printf '%s' "после --domain нужен адрес." ;;
        need_cert) printf '%s' "после --cert нужен путь к файлу." ;;
        need_key) printf '%s' "после --key нужен путь к файлу." ;;
        need_admin_email) printf '%s' "после --admin-email нужен адрес." ;;
        unknown_param) printf '%s' "неизвестный параметр: %s" ;;
        not_cert) printf '%s' "%s — не сертификат в формате PEM." ;;
        not_key) printf '%s' "%s — не закрытый ключ в формате PEM (ключи с паролем не поддерживаются)." ;;
        key_mismatch) printf '%s' "ключ %s не подходит к сертификату %s." ;;
        cert_expired) printf '%s' "сертификат %s истёк. Положите действующий или удалите оба файла в %s/, чтобы выпустить новый." ;;
        no_docker) printf '%s' "Docker не найден. Установите Docker Engine: https://docs.docker.com/engine/install/" ;;
        docker_unreachable) printf '%s' "нет связи с Docker: демон не запущен или нет прав. Запустите установщик от root или добавьте пользователя в группу docker." ;;
        no_compose) printf '%s' "не найден плагин Docker Compose (команда 'docker compose'). Установите docker-compose-plugin." ;;
        no_openssl) printf '%s' "openssl не найден: он нужен для генерации секретов и сертификата." ;;
        no_template) printf '%s' "не найден шаблон %s: запускайте установщик из корня репозитория." ;;
        no_compose_file) printf '%s' "не найден %s: запускайте установщик из корня репозитория." ;;
        bad_admin_email) printf '%s' "некорректный email администратора: %s" ;;
        cert_and_key_together) printf '%s' "--cert и --key указываются вместе." ;;
        cert_file_missing) printf '%s' "файл сертификата не найден: %s" ;;
        key_file_missing) printf '%s' "файл ключа не найден: %s" ;;
        certs_present) printf '%s' "в %s/ уже есть сертификат — свой не заменит его молча. Удалите %s и %s осознанно и запустите снова." ;;
        half_pair) printf '%s' "в %s/ нет файла %s, хотя второй из пары есть. Положите пару целиком или удалите оставшийся файл." ;;
        env_exists) printf '%s' "Файл %s уже существует — оставляю без изменений." ;;
        env_open_perms) printf '%s' "Внимание: %s доступен не только владельцу. Закройте доступ: chmod 600 %s" ;;
        domain_from_env) printf '%s' "Адрес панели берётся из %s (PANEL_DOMAIN=%s); параметр --domain игнорируется." ;;
        bad_domain) printf '%s' "некорректный домен. Допустимы буквы, цифры, точки и дефисы (например, panel.example.com или 203.0.113.5)." ;;
        orphan_volume) printf '%s' "найден том с данными прошлой установки (%s), а файла %s нет. Новые пароль БД и ключ шифрования к ним не подойдут. Восстановите %s из копии. Если данные не нужны — удалите том осознанно: docker volume rm %s" ;;
        ask_domain) printf '%s' "Домен или IP панели [%s]: " ;;
        template_lacks_key) printf '%s' "в %s нет строки %s=… — установщик и шаблон разошлись." ;;
        template_placeholders) printf '%s' "в %s остались заглушки change_me, которые установщик не умеет заполнять." ;;
        selfsigned_failed) printf '%s' "не удалось выпустить самоподписанный сертификат для «%s»." ;;
        selfsigned_issued) printf '%s' "Выпущен самоподписанный сертификат для %s (до: %s)." ;;
        selfsigned_browser) printf '%s' "Браузер предупредит, что не доверяет ему, — для пробного запуска это нормально." ;;
        fingerprint) printf '%s' "Отпечаток SHA-256 (сверьте с тем, что покажет браузер): %s" ;;
        own_cert_hint) printf '%s' "Для боевого домена используйте свой сертификат: ./install.sh --cert fullchain.pem --key privkey.pem" ;;
        own_cert_hint2) printf '%s' "(или положите файлы в %s/ вручную: %s и %s)." ;;
        own_installed) printf '%s' "Ваш сертификат установлен в %s/ (до: %s)." ;;
        cert_found) printf '%s' "Сертификат в %s/ найден — оставляю без изменений (до: %s)." ;;
        cert_expiring) printf '%s' "Внимание: сертификат скоро истекает (%s). Обновите файлы в %s/ и перезапустите nginx." ;;
        cert_not_covering) printf '%s' "Внимание: сертификат не покрывает адрес %s — браузер покажет ошибку. Проверьте домен сертификата." ;;
        env_created) printf '%s' "Создан %s (доступ только владельцу): случайные пароль БД, JWT-секрет и ключ шифрования." ;;
        backup_warn) printf '%s' "ВАЖНО: сохраните копию %s отдельно от этого сервера (менеджер паролей, сейф)." ;;
        key_warn) printf '%s' "ENCRYPTION_KEY нельзя ни потерять, ни сменить: им зашифрованы SSH-учётки и VPN-конфиги в базе." ;;
        no_start) printf '%s' "Запуск пропущен (--no-start). Когда будете готовы: %s up -d --build" ;;
        bad_wait) printf '%s' "INSTALL_WAIT_SECONDS=«%s» — нужно целое число секунд больше нуля." ;;
        bad_port) printf '%s' "%s=«%s» — это не номер порта (1–65535)." ;;
        port_busy) printf '%s' "порт %s уже занят другой программой. Освободите его или задайте другой в %s (HTTP_PORT, HTTPS_PORT) и запустите установщик снова." ;;
        starting) printf '%s' "Запускаю панель. Первая сборка образов занимает несколько минут…" ;;
        starting_nobuild) printf '%s' "Запускаю панель из образов, уже загруженных на этой машине (без сборки)…" ;;
        no_local_images) printf '%s' "нет образов панели на этой машине (%s). Привезите их с другого компьютера: ./deploy/ship-images.sh ЭТОТ_СЕРВЕР" ;;
        up_failed) printf '%s' "не удалось запустить панель. Что случилось: %s logs --tail 100" ;;
        waiting) printf '%s' "Жду готовности панели (до %s с)…" ;;
        none) printf '%s' "нет" ;;
        api_unhealthy) printf '%s' "контейнер api не стал готовым за %s с (статус: %s). Последние строки логов выше; полные: %s logs" ;;
        https_down) printf '%s' "api готов, но https://127.0.0.1:%s/health не отвечает по HTTPS: смотрите nginx и сертификат. Полные логи: %s logs" ;;
        admin_header) printf '%s' "Создаём первого администратора (если админов ещё нет):" ;;
        admin_failed) printf '%s' "не удалось создать администратора. Повторите: %s" ;;
        admin_skipped) printf '%s' "Администратор не создан: для ввода пароля нужен терминал. Создайте его командой:" ;;
        started) printf '%s' "Панель запущена: %s" ;;
        selfsigned_note) printf '%s' "Сертификат самоподписанный: браузер предупредит, что не доверяет ему, — для пробного запуска это нормально." ;;
        logs_line) printf '%s' "Логи:      %s logs -f" ;;
        stop_line) printf '%s' "Остановка: %s down" ;;
        backup_reminder) printf '%s' "Не забудьте сохранить копию %s отдельно от этого сервера: без ENCRYPTION_KEY данные в базе не прочитать." ;;
    esac
}

msg_en() {
    case $1 in
        need_domain) printf '%s' "--domain needs an address." ;;
        need_cert) printf '%s' "--cert needs a file path." ;;
        need_key) printf '%s' "--key needs a file path." ;;
        need_admin_email) printf '%s' "--admin-email needs an address." ;;
        unknown_param) printf '%s' "unknown option: %s" ;;
        not_cert) printf '%s' "%s is not a PEM certificate." ;;
        not_key) printf '%s' "%s is not a PEM private key (keys protected by a passphrase are not supported)." ;;
        key_mismatch) printf '%s' "key %s does not match certificate %s." ;;
        cert_expired) printf '%s' "certificate %s has expired. Put a valid one in place or delete both files in %s/ to issue a new one." ;;
        no_docker) printf '%s' "Docker not found. Install Docker Engine: https://docs.docker.com/engine/install/" ;;
        docker_unreachable) printf '%s' "cannot talk to Docker: the daemon is not running or you lack permission. Run the installer as root or add your user to the docker group." ;;
        no_compose) printf '%s' "the Docker Compose plugin ('docker compose') was not found. Install docker-compose-plugin." ;;
        no_openssl) printf '%s' "openssl not found: it is needed to generate secrets and the certificate." ;;
        no_template) printf '%s' "template %s not found: run the installer from the repository root." ;;
        no_compose_file) printf '%s' "%s not found: run the installer from the repository root." ;;
        bad_admin_email) printf '%s' "invalid administrator email: %s" ;;
        cert_and_key_together) printf '%s' "--cert and --key must be given together." ;;
        cert_file_missing) printf '%s' "certificate file not found: %s" ;;
        key_file_missing) printf '%s' "key file not found: %s" ;;
        certs_present) printf '%s' "%s/ already holds a certificate - your own will not silently replace it. Delete %s and %s deliberately and run again." ;;
        half_pair) printf '%s' "%s/ has no %s although the other file of the pair is there. Provide the whole pair or delete the remaining file." ;;
        env_exists) printf '%s' "%s already exists - leaving it unchanged." ;;
        env_open_perms) printf '%s' "Warning: %s is accessible to more than its owner. Restrict it: chmod 600 %s" ;;
        domain_from_env) printf '%s' "The panel address comes from %s (PANEL_DOMAIN=%s); --domain is ignored." ;;
        bad_domain) printf '%s' "invalid domain. Letters, digits, dots and hyphens only (for example panel.example.com or 203.0.113.5)." ;;
        orphan_volume) printf '%s' "found a data volume from a previous installation (%s) but there is no %s. A new database password and encryption key will not fit it. Restore %s from a backup. If the data is not needed, delete the volume deliberately: docker volume rm %s" ;;
        ask_domain) printf '%s' "Panel domain or IP [%s]: " ;;
        template_lacks_key) printf '%s' "%s has no %s=... line - the installer and the template are out of sync." ;;
        template_placeholders) printf '%s' "%s still contains change_me placeholders the installer cannot fill in." ;;
        selfsigned_failed) printf '%s' "could not issue a self-signed certificate for \"%s\"." ;;
        selfsigned_issued) printf '%s' "Issued a self-signed certificate for %s (valid until: %s)." ;;
        selfsigned_browser) printf '%s' "The browser will warn that it does not trust it - that is fine for a trial run." ;;
        fingerprint) printf '%s' "SHA-256 fingerprint (compare with what the browser shows): %s" ;;
        own_cert_hint) printf '%s' "For a real domain use your own certificate: ./install.sh --cert fullchain.pem --key privkey.pem" ;;
        own_cert_hint2) printf '%s' "(or put the files into %s/ by hand: %s and %s)." ;;
        own_installed) printf '%s' "Your certificate is installed in %s/ (valid until: %s)." ;;
        cert_found) printf '%s' "Found a certificate in %s/ - leaving it unchanged (valid until: %s)." ;;
        cert_expiring) printf '%s' "Warning: the certificate expires soon (%s). Update the files in %s/ and restart nginx." ;;
        cert_not_covering) printf '%s' "Warning: the certificate does not cover %s - the browser will show an error. Check the certificate's domain." ;;
        env_created) printf '%s' "Created %s (owner-only access): a random database password, JWT secret and encryption key." ;;
        backup_warn) printf '%s' "IMPORTANT: keep a copy of %s away from this server (password manager, safe)." ;;
        key_warn) printf '%s' "ENCRYPTION_KEY must be neither lost nor changed: SSH credentials and VPN configs in the database are encrypted with it." ;;
        no_start) printf '%s' "Start skipped (--no-start). When you are ready: %s up -d --build" ;;
        bad_wait) printf '%s' "INSTALL_WAIT_SECONDS=\"%s\" must be a whole number of seconds greater than zero." ;;
        bad_port) printf '%s' "%s=\"%s\" is not a port number (1-65535)." ;;
        port_busy) printf '%s' "port %s is already in use by another program. Free it or set another in %s (HTTP_PORT, HTTPS_PORT) and run the installer again." ;;
        starting) printf '%s' "Starting the panel. The first image build takes a few minutes..." ;;
        starting_nobuild) printf '%s' "Starting the panel from the images already on this machine (no build)..." ;;
        no_local_images) printf '%s' "the panel images are missing on this machine (%s). Bring them from another computer: ./deploy/ship-images.sh THIS_SERVER" ;;
        up_failed) printf '%s' "could not start the panel. What happened: %s logs --tail 100" ;;
        waiting) printf '%s' "Waiting for the panel to be ready (up to %s s)..." ;;
        none) printf '%s' "none" ;;
        api_unhealthy) printf '%s' "the api container did not become ready within %s s (status: %s). The last log lines are above; full logs: %s logs" ;;
        https_down) printf '%s' "api is ready but https://127.0.0.1:%s/health does not answer over HTTPS: check nginx and the certificate. Full logs: %s logs" ;;
        admin_header) printf '%s' "Creating the first administrator (if there are none yet):" ;;
        admin_failed) printf '%s' "could not create the administrator. Retry: %s" ;;
        admin_skipped) printf '%s' "No administrator was created: a terminal is needed to enter the password. Create one with:" ;;
        started) printf '%s' "The panel is running: %s" ;;
        selfsigned_note) printf '%s' "The certificate is self-signed: the browser will warn that it does not trust it - fine for a trial run." ;;
        logs_line) printf '%s' "Logs:      %s logs -f" ;;
        stop_line) printf '%s' "Stop:      %s down" ;;
        backup_reminder) printf '%s' "Do not forget to keep a copy of %s away from this server: without ENCRYPTION_KEY the data in the database cannot be read." ;;
    esac
}

usage() {
    if [ "$lang" = ru ]; then
        cat <<'EOF'
Использование: ./install.sh [--domain ДОМЕН_ИЛИ_IP] [--cert ФАЙЛ --key ФАЙЛ] [--admin-email EMAIL] [--no-start] [--no-admin] [--no-build]

  --domain   адрес, по которому будет открываться панель (по умолчанию localhost);
             по нему выпускается сертификат. Без параметра в терминале спросит.
  --cert     свой сертификат с цепочкой (PEM), копируется в certs/fullchain.pem;
  --key      его закрытый ключ (PEM, без пароля), копируется в certs/privkey.pem.
             Без них выпускается самоподписанный сертификат (браузер предупредит, что ему не доверяет).
  --admin-email  email первого администратора (иначе спросит в терминале);
  --no-start     только подготовить .env.prod и сертификат, панель не запускать;
  --no-admin     не предлагать создать администратора (так вызывает ./panel update);
  --no-build     не собирать образы, взять уже загруженные (для серверов, куда образы привозят
                 с другого компьютера: ./deploy/ship-images.sh);
  -h, --help эта справка

Переменная INSTALL_WAIT_SECONDS (по умолчанию 300) — сколько ждать готовности панели после запуска.
Язык сообщений: PANEL_LANG=ru|en, иначе по локали системы (LC_ALL, LC_MESSAGES, LANG), по умолчанию английский.

Существующие .env.prod и certs/*.pem не изменяются: чтобы заменить сертификат, удалите файлы осознанно.
EOF
    else
        cat <<'EOF'
Usage: ./install.sh [--domain DOMAIN_OR_IP] [--cert FILE --key FILE] [--admin-email EMAIL] [--no-start] [--no-admin] [--no-build]

  --domain   address the panel will be opened at (default localhost);
             the certificate is issued for it. Asked in a terminal if omitted.
  --cert     your own certificate with its chain (PEM), copied to certs/fullchain.pem;
  --key      its private key (PEM, no passphrase), copied to certs/privkey.pem.
             Without them a self-signed certificate is issued (the browser will warn that it does not trust it).
  --admin-email  email of the first administrator (asked in a terminal otherwise);
  --no-start     only prepare .env.prod and the certificate, do not start the panel;
  --no-admin     do not offer to create an administrator (./panel update uses this);
  --no-build     do not build the images, use the ones already loaded (for servers that get the
                 images from another computer: ./deploy/ship-images.sh);
  -h, --help     this help

INSTALL_WAIT_SECONDS (default 300) is how long to wait for the panel to become ready after the start.
Message language: PANEL_LANG=ru|en, otherwise the system locale (LC_ALL, LC_MESSAGES, LANG), English by default.

Existing .env.prod and certs/*.pem are never modified: to replace the certificate, delete the files deliberately.
EOF
    fi
}

domain=""
domain_given=0
cert_src=""
key_src=""
admin_email=""
start=1
ask_admin=1
build=1
while [ $# -gt 0 ]; do
    case "$1" in
        --domain)
            [ $# -ge 2 ] || die "$(t need_domain)"
            domain=$2
            domain_given=1
            shift 2
            ;;
        --cert)
            [ $# -ge 2 ] || die "$(t need_cert)"
            cert_src=$2
            shift 2
            ;;
        --key)
            [ $# -ge 2 ] || die "$(t need_key)"
            key_src=$2
            shift 2
            ;;
        --admin-email)
            [ $# -ge 2 ] || die "$(t need_admin_email)"
            admin_email=$2
            shift 2
            ;;
        --no-start)
            start=0
            shift
            ;;
        --no-admin)
            ask_admin=0
            shift
            ;;
        --no-build)
            build=0
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

valid_domain='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'
ipv4='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

is_valid_domain() { [[ $1 =~ $valid_domain ]] && [[ $1 != *..* ]]; }

# Проверяет пару «сертификат + закрытый ключ»: оба читаются, ключ подходит к сертификату, срок не вышел.
check_pair() {
    local cert=$1 key=$2 cert_pub key_pub
    openssl x509 -in "$cert" -noout > /dev/null 2>&1 || die "$(t not_cert "$cert")"
    # -passin pass: — ключ с паролем не должен заставить openssl ждать ввода: он просто не прочитается.
    openssl pkey -in "$key" -passin pass: -noout > /dev/null 2>&1 \
        || die "$(t not_key "$key")"
    cert_pub=$(openssl x509 -in "$cert" -noout -pubkey)
    key_pub=$(openssl pkey -in "$key" -passin pass: -pubout)
    [ "$cert_pub" = "$key_pub" ] || die "$(t key_mismatch "$key" "$cert")"
    openssl x509 -in "$cert" -noout -checkend 0 > /dev/null || die "$(t cert_expired "$cert" "$CERT_DIR")"
}

# --- проверка окружения ---------------------------------------------------------------------------------------------

command -v docker > /dev/null 2>&1 || die "$(t no_docker)"
docker info > /dev/null 2>&1 \
    || die "$(t docker_unreachable)"
docker compose version > /dev/null 2>&1 \
    || die "$(t no_compose)"
command -v openssl > /dev/null 2>&1 || die "$(t no_openssl)"
[ -f "$TEMPLATE" ] || die "$(t no_template "$TEMPLATE")"
[ -f "$COMPOSE_FILE" ] || die "$(t no_compose_file "$COMPOSE_FILE")"
# Адрес уходит аргументом команды, но проверяется строго: опечатку лучше поймать до записи секретов.
if [ -n "$admin_email" ] && ! [[ $admin_email =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    die "$(t bad_admin_email "$admin_email")"
fi

# --- сертификат: что делать, решаем ДО записи чего-либо ----------------------------------------------------------------
# Ошибка в сертификате не должна оставить после себя .env.prod с уже сгенерированными секретами.

if [ -n "$cert_src" ] || [ -n "$key_src" ]; then
    { [ -n "$cert_src" ] && [ -n "$key_src" ]; } || die "$(t cert_and_key_together)"
    [ -f "$cert_src" ] || die "$(t cert_file_missing "$cert_src")"
    [ -f "$key_src" ] || die "$(t key_file_missing "$key_src")"
    if [ -e "$CERT_FILE" ] || [ -e "$KEY_FILE" ]; then
        die "$(t certs_present "$CERT_DIR" "$CERT_FILE" "$KEY_FILE")"
    fi
    check_pair "$cert_src" "$key_src"
    cert_mode=own
elif [ -e "$CERT_FILE" ] && [ -e "$KEY_FILE" ]; then
    check_pair "$CERT_FILE" "$KEY_FILE"
    cert_mode=existing
elif [ -e "$CERT_FILE" ] || [ -e "$KEY_FILE" ]; then
    [ -e "$CERT_FILE" ] && missing=$KEY_FILE || missing=$CERT_FILE
    die "$(t half_pair "$CERT_DIR" "$missing")"
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
    say "$(t env_exists "$ENV_FILE")"
    if [ -n "$(find "$ENV_FILE" -maxdepth 0 -perm /077 2> /dev/null)" ]; then
        say "$(t env_open_perms "$ENV_FILE" "$ENV_FILE")"
    fi
    from_env=$(env_domain)
    if [ -n "$from_env" ]; then
        if [ "$domain_given" -eq 1 ] && [ "$domain" != "$from_env" ]; then
            say "$(t domain_from_env "$ENV_FILE" "$from_env")"
        fi
        domain=$from_env
    elif [ "$domain_given" -eq 1 ]; then
        is_valid_domain "$domain" || die "$(t bad_domain)"
    else
        domain=$DEFAULT_DOMAIN
    fi
    env_created=0
else
    if [ -n "$(docker volume ls -q --filter "name=^${DB_VOLUME}\$" 2> /dev/null)" ]; then
        die "$(t orphan_volume "$DB_VOLUME" "$ENV_FILE" "$ENV_FILE" "$DB_VOLUME")"
    fi

    # Домен попадает в сертификат и в .env.prod, поэтому проверяется строго: только буквы, цифры, точки и дефисы.
    if [ "$domain_given" -eq 0 ]; then
        domain=$DEFAULT_DOMAIN
        if [ -t 0 ] && [ -t 1 ]; then
            read -r -p "$(t ask_domain "$DEFAULT_DOMAIN")" answer || answer=""
            domain=${answer:-$DEFAULT_DOMAIN}
        fi
    fi
    is_valid_domain "$domain" || die "$(t bad_domain)"

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
            *) die "$(t template_lacks_key "$TEMPLATE" "$key")" ;;
        esac
    done
    if grep -q 'change_me' "$tmp"; then
        die "$(t template_placeholders "$TEMPLATE")"
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
        || die "$(t selfsigned_failed "$domain")"
    install_pair "$issue_dir/fullchain.pem" "$issue_dir/privkey.pem"
    rm -rf "$issue_dir"
    issue_dir=""
}

case $cert_mode in
    generate)
        issue_self_signed
        fingerprint=$(openssl x509 -in "$CERT_FILE" -noout -fingerprint -sha256 | cut -d= -f2)
        say "$(t selfsigned_issued "$domain" "$(cert_expiry "$CERT_FILE")")"
        say "$(t selfsigned_browser)"
        say "$(t fingerprint "$fingerprint")"
        say "$(t own_cert_hint)"
        say "$(t own_cert_hint2 "$CERT_DIR" "$CERT_FILE" "$KEY_FILE")"
        ;;
    own)
        install_pair "$cert_src" "$key_src"
        say "$(t own_installed "$CERT_DIR" "$(cert_expiry "$CERT_FILE")")"
        ;;
    existing)
        say "$(t cert_found "$CERT_DIR" "$(cert_expiry "$CERT_FILE")")"
        ;;
esac

if [ "$cert_mode" != generate ]; then
    openssl x509 -in "$CERT_FILE" -noout -checkend "$WARN_EXPIRY_SECONDS" > /dev/null \
        || say "$(t cert_expiring "$(cert_expiry "$CERT_FILE")" "$CERT_DIR")"
    if [[ $domain =~ $ipv4 ]]; then
        covers=$(openssl x509 -in "$CERT_FILE" -noout -checkip "$domain" 2> /dev/null || true)
    else
        covers=$(openssl x509 -in "$CERT_FILE" -noout -checkhost "$domain" 2> /dev/null || true)
    fi
    case $covers in
        *"does match"*) ;;
        *) say "$(t cert_not_covering "$domain")" ;;
    esac
fi

if [ "$env_created" -eq 1 ]; then
    say ""
    say "$(t env_created "$ENV_FILE")"
    say ""
    say "$(t backup_warn "$ENV_FILE")"
    say "$(t key_warn)"
fi

# --- запуск --------------------------------------------------------------------------------------------------------

compose=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
compose_text="docker compose --env-file $ENV_FILE -f $COMPOSE_FILE"

if [ "$start" -eq 0 ]; then
    say ""
    say "$(t no_start "$compose_text")"
    exit 0
fi

wait_seconds=${INSTALL_WAIT_SECONDS:-300}
[[ $wait_seconds =~ ^[1-9][0-9]*$ ]] || die "$(t bad_wait "$wait_seconds")"

# Порт: переменная окружения (compose читает и её, она важнее файла), затем .env.prod, затем умолчание.
port_setting() {
    local name=$1 value
    value=${!name:-}
    [ -n "$value" ] || value=$(env_get "$name")
    value=${value:-$2}
    if ! [[ $value =~ ^[0-9]{1,5}$ ]] || [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
        die "$(t bad_port "$name" "$value")"
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
            die "$(t port_busy "$port" "$ENV_FILE")"
        fi
    done
fi

say ""
if [ "$build" -eq 1 ]; then
    say "$(t starting)"
    "${compose[@]}" up -d --build || die "$(t up_failed "$compose_text")"
else
    # Без сборки образы панели должны уже лежать на машине (их привозит ship-images.sh): иначе compose полез бы за
    # ними в реестр, а не нашёл бы — лучше сказать об этом прямо.
    missing=""
    for image in vpn-panel:latest vpn-panel-web:latest; do
        docker image inspect "$image" > /dev/null 2>&1 || missing="$missing $image"
    done
    [ -z "$missing" ] || die "$(t no_local_images "${missing# }")"
    say "$(t starting_nobuild)"
    "${compose[@]}" up -d || die "$(t up_failed "$compose_text")"
fi

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

say "$(t waiting "$wait_seconds")"
if ! wait_ready; then
    "${compose[@]}" logs --tail 30 api nginx 2>&1 | sed 's/^/  | /' >&2 || true
    if [ "$api_status" != healthy ]; then
        die "$(t api_unhealthy "$wait_seconds" "${api_status:-$(t none)}" "$compose_text")"
    fi
    die "$(t https_down "$https_port" "$compose_text")"
fi

# --- первый администратор ------------------------------------------------------------------------------------------

if [ "$ask_admin" -eq 1 ]; then
    admin_cmd=("${compose[@]}" run --rm -e "PANEL_LANG=$lang" api python -m scripts.create_admin --only-if-empty)
    [ -z "$admin_email" ] || admin_cmd+=("$admin_email")
    admin_text=$(printf '%q ' "${admin_cmd[@]}")
    admin_text=${admin_text% }

    say ""
    if [ -t 0 ] && [ -t 1 ]; then
        say "$(t admin_header)"
        "${admin_cmd[@]}" || die "$(t admin_failed "$admin_text")"
    else
        say "$(t admin_skipped)"
        say "  $admin_text"
    fi
fi

# --- итог ----------------------------------------------------------------------------------------------------------

url="https://$domain"
[ "$https_port" -eq 443 ] || url="$url:$https_port"
say ""
say "$(t started "$url")"
if [ "$cert_mode" = generate ]; then
    say "$(t selfsigned_note)"
fi
say "$(t logs_line "$compose_text")"
say "$(t stop_line "$compose_text")"
if [ "$env_created" -eq 1 ]; then
    say ""
    say "$(t backup_reminder "$ENV_FILE")"
fi
