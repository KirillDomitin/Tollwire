# Общее для install.sh и panel: язык сообщений, t (сообщение по ключу), say и die.
# Подключается командой `source deploy/common.sh`. Скрипт-хозяин сам определяет msg_ru и msg_en — таблицы
# «ключ → строка для printf»; подстановки %s идут по порядку, одинаковому в обоих языках.

# Язык сообщений: PANEL_LANG (ru | en), иначе по локали системы (LC_ALL, LC_MESSAGES, LANG), иначе английский.
# Тот же PANEL_LANG передаётся в контейнер при создании администратора — скрипт говорит на языке установщика.
lang=${PANEL_LANG:-}
if [ -z "$lang" ]; then
    for locale_value in "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
        if [ -n "$locale_value" ]; then
            lang=$locale_value
            break
        fi
    done
fi
case $lang in
    ru*) lang=ru ;;
    *) lang=en ;;
esac

# Общие библиотеки (deploy/letsencrypt.sh, deploy/bot.sh) пишут свои сообщения в lib_msg_ru / lib_msg_en, а в конце
# вызывают `lib_register ИМЯ`: функции переименовываются в ИМЯ_msg_ru / ИМЯ_msg_en, чтобы несколько библиотек в одном
# процессе не затирали друг друга. Ключи сообщений у библиотек разные (префикс: le_, bot_).
LIB_MSG_PROVIDERS=()
lib_register() {
    local name=$1 l body
    for l in ru en; do
        body=$(declare -f "lib_msg_$l")
        eval "${body/lib_msg_$l/${name}_msg_$l}"
        unset -f "lib_msg_$l"
    done
    LIB_MSG_PROVIDERS+=("$name")
}

t() {
    local key=$1 fmt provider
    shift
    if [ "$lang" = ru ]; then fmt=$(msg_ru "$key"); else fmt=$(msg_en "$key"); fi
    # Сообщения общих библиотек — запасной источник, если у скрипта-хозяина такого ключа нет.
    if [ -z "$fmt" ]; then
        for provider in ${LIB_MSG_PROVIDERS[@]+"${LIB_MSG_PROVIDERS[@]}"}; do
            fmt=$("${provider}_msg_$lang" "$key")
            [ -z "$fmt" ] || break
        done
    fi
    # shellcheck disable=SC2059  # формат — наша собственная таблица выше
    printf -- "$fmt" "$@"
}

say() { printf '%s\n' "$*"; }

# Значение переменной из готового $ENV_FILE; комментарий после значения и кавычки отбрасываются.
env_get() {
    local value
    value=$(grep -m1 "^$1=" "$ENV_FILE" 2> /dev/null | cut -d= -f2- || true)
    value=${value%%#*}
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value=${value#[\"\']}
    value=${value%[\"\']}
    printf '%s' "$value"
}

# Меняет в $ENV_FILE одну строку КЛЮЧ=значение (или дописывает, если её нет); остальные строки остаются как были.
# Файл подменяется целиком и атомарно (временный файл рядом + mv), права 0600: рядом лежат секреты.
env_set() {
    local key=$1 value=$2 tmp found=0 line
    tmp=$(umask 077 && mktemp "$ENV_FILE.XXXXXX")
    while IFS= read -r line || [ -n "$line" ]; do
        case $line in
            "$key="*)
                printf '%s=%s
' "$key" "$value"
                found=1
                ;;
            *) printf '%s
' "$line" ;;
        esac
    done < "$ENV_FILE" > "$tmp"
    [ "$found" -eq 1 ] || printf '%s=%s
' "$key" "$value" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
}

# Слушает ли кто-нибудь локальный порт $1 (проверка соединением, без внешних утилит).
port_busy() { (exec 3<> "/dev/tcp/127.0.0.1/$1") 2> /dev/null; }
die() {
    if [ "$lang" = ru ]; then printf 'Ошибка: %s\n' "$*" >&2; else printf 'Error: %s\n' "$*" >&2; fi
    exit 1
}

