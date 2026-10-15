#!/usr/bin/env bash
# Установщик VPN Control Panel (прод, Docker).
#
#   ./install.sh [--domain panel.example.com]
#
# Сейчас делает первый шаг: проверяет окружение и создаёт .env.prod со случайными секретами.
# TLS-сертификат, запуск и создание админа — следующие шаги (пока вручную, см. INSTALL.md).
#
# Безопасно запускать повторно: существующий .env.prod не трогается никогда. Новый ENCRYPTION_KEY сделал бы
# нечитаемыми все SSH-учётки и VPN-конфиги в базе. Секреты на экран не выводятся.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

ENV_FILE=.env.prod
TEMPLATE=.env.prod.example
# Том базы из docker-compose.prod.yml (проект vpn-panel). Если он есть, а .env.prod нет — прошлая установка
# потеряла свои секреты, и новые к этой базе не подойдут.
DB_VOLUME=vpn-panel_pg_data
DEFAULT_DOMAIN=localhost

say() { printf '%s\n' "$*"; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Использование: ./install.sh [--domain ДОМЕН_ИЛИ_IP]

  --domain   адрес, по которому будет открываться панель (по умолчанию localhost);
             по нему установщик выпустит сертификат. Без параметра в терминале спросит.
  -h, --help эта справка

Существующий .env.prod не изменяется.
EOF
}

domain=""
domain_given=0
while [ $# -gt 0 ]; do
    case "$1" in
        --domain)
            [ $# -ge 2 ] || die "после --domain нужен адрес."
            domain=$2
            domain_given=1
            shift 2
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

# --- проверка окружения ---------------------------------------------------------------------------------------------

command -v docker > /dev/null 2>&1 || die "Docker не найден. Установите Docker Engine: https://docs.docker.com/engine/install/"
docker info > /dev/null 2>&1 \
    || die "нет связи с Docker: демон не запущен или нет прав. Запустите установщик от root или добавьте пользователя в группу docker."
docker compose version > /dev/null 2>&1 \
    || die "не найден плагин Docker Compose (команда 'docker compose'). Установите docker-compose-plugin."
command -v openssl > /dev/null 2>&1 || die "openssl не найден: он нужен для генерации секретов."
[ -f "$TEMPLATE" ] || die "не найден шаблон $TEMPLATE: запускайте установщик из корня репозитория."

# --- .env.prod -----------------------------------------------------------------------------------------------------

if [ -e "$ENV_FILE" ]; then
    say "Файл $ENV_FILE уже существует — оставляю без изменений."
    if [ -n "$(find "$ENV_FILE" -maxdepth 0 -perm /077 2> /dev/null)" ]; then
        say "Внимание: $ENV_FILE доступен не только владельцу. Закройте доступ: chmod 600 $ENV_FILE"
    fi
    exit 0
fi

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
valid_domain='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'
if ! [[ $domain =~ $valid_domain ]] || [[ $domain == *..* ]]; then
    die "некорректный домен. Допустимы буквы, цифры, точки и дефисы (например, panel.example.com или 203.0.113.5)."
fi

postgres_password=$(openssl rand -hex 32)
jwt_secret=$(openssl rand -hex 32)
# Ключ Fernet — 32 случайных байта в base64url (44 знака).
encryption_key=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')

tmp=$(umask 077 && mktemp "${ENV_FILE}.XXXXXX")
trap 'rm -f "$tmp"' EXIT

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
trap - EXIT

say "Создан $ENV_FILE (доступ только владельцу): случайные пароль БД, JWT-секрет и ключ шифрования."
say ""
say "ВАЖНО: сохраните копию $ENV_FILE отдельно от этого сервера (менеджер паролей, сейф)."
say "ENCRYPTION_KEY нельзя ни потерять, ни сменить: им зашифрованы SSH-учётки и VPN-конфиги в базе."
say ""
say "Дальше (пока вручную, см. INSTALL.md): сертификат для $domain в certs/ и запуск через docker compose."
