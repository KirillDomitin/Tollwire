# Telegram-бот на сервере панели: общий код install.sh и ./panel (включить, выключить, сменить ключ, ссылка для второго
# сервера). Подключается командой `source deploy/bot.sh` после deploy/common.sh. Хозяин задаёт ENV_FILE (.env.prod),
# массив compose и (необязательно) bot_build=0 (--no-build: образ бота уже привезён ship-images.sh).
#
# Секреты. Токен Telegram и ключ бота лежат в отдельном .env.bot (0600): контейнеры api, worker и migrate их не получают.
# В .env.prod попадает только COMPOSE_PROFILES=bot (иначе `docker compose down` без профиля не тронул бы бота).
# Токен и ключ не передаются аргументами команд (их видно в `ps`): curl читает их из stdin (`-K -`), ключ для
# revoke/seen подаётся в stdin скрипта, ввод токена скрытый. На экран ключ не выводится, кроме `bot key --remote`:
# там его показывают один раз и только человеку в терминале.

BOT_ENV_FILE=.env.bot
BOT_IMAGE=vpn-panel-bot:latest
BOT_KEY_NAME=panel-bot
BOT_SEEN_WITHIN=90 # бот «на связи», если его ключ выходил на связь не позже этого числа секунд назад
bot_build=1

lib_msg_ru() {
    case $1 in
        bot_choice_invalid) printf '%s' "--bot: допустимы local, remote, none (получено: «%s»)." ;;
        bot_menu_title) printf '%s' "Telegram-бот (пользователи сами получают VPN по приглашению):" ;;
        bot_menu_local) printf '%s' "  1) на этом сервере" ;;
        bot_menu_remote) printf '%s' "  2) на другом сервере (выдам команду для установки)" ;;
        bot_menu_none) printf '%s' "  3) не нужен (по умолчанию; добавить позже: ./panel bot enable)" ;;
        bot_menu_choice) printf '%s' "Выбор [3]: " ;;
        bot_menu_again) printf '%s' "Введите 1, 2 или 3." ;;
        bot_old_compose) printf '%s' "для бота нужен Docker Compose 2.24 или новее (сейчас %s)." ;;
        bot_no_token) printf '%s' "нужен токен бота от @BotFather: задайте переменную TELEGRAM_BOT_TOKEN или запустите в терминале." ;;
        bot_bad_token) printf '%s' "это не похоже на токен бота (ожидается вид 123456789:AAF… от @BotFather)." ;;
        bot_token_prompt) printf '%s' "Токен бота от @BotFather (ввод скрыт): " ;;
        bot_telegram_failed) printf '%s' "Telegram не принял токен или недоступен с этого сервера. Если Telegram отсюда не открывается, запустите бота на другом сервере: %s" ;;
        bot_key_failed) printf '%s' "не удалось выпустить ключ бота. Повторите после запуска панели: ./panel bot enable" ;;
        bot_env_written) printf '%s' "Настройки бота записаны в %s (доступ только владельцу)." ;;
        bot_already) printf '%s' "Бот уже настроен (%s): токен и ключ не меняю." ;;
        bot_missing_image) printf '%s' "нет образа бота (%s). Привезите его с другого компьютера: ./deploy/ship-images.sh ЭТОТ_СЕРВЕР" ;;
        bot_starting) printf '%s' "Запускаю бота…" ;;
        bot_start_failed) printf '%s' "не удалось запустить бота. Что случилось: ./panel logs bot" ;;
        bot_waiting) printf '%s' "Жду, пока бот выйдет на связь с панелью (до %s с)…" ;;
        bot_running) printf '%s' "Бот работает и на связи с панелью." ;;
        bot_silent) printf '%s' "Бот пока не вышел на связь с панелью. Посмотрите, что он пишет: ./panel logs bot" ;;
        bot_no_start) printf '%s' "Бот не настроен (--no-start). Когда панель будет запущена: ./panel bot enable" ;;
        bot_remote_no_start) printf '%s' "Ключ для бота на другом сервере (--no-start) не выпускался. Когда панель будет запущена: ./panel bot key --remote" ;;
        bot_remote_no_terminal) printf '%s' "Ключ бота можно показать только в терминале (иначе он осядет в логе). Выпустите его там: ./panel bot key --remote" ;;
        bot_remote_title) printf '%s' "Бот на другом сервере. Выполните там (нужны Docker и git):" ;;
        bot_remote_key_title) printf '%s' "Ключ бота — показан один раз, установка спросит его скрытым вводом:" ;;
        bot_remote_token_note) printf '%s' "Токен Telegram (от @BotFather) установка спросит там же." ;;
        bot_remote_repo_unknown) printf '%s' "<адрес репозитория>" ;;
        bot_remote_old_key) printf '%s' "Если это замена ключа, отзовите старый после перенастройки бота: ./panel bot revoke" ;;
        bot_disabled) printf '%s' "Бот остановлен и убран из запуска. Настройки (%s) сохранены; ключ остаётся действующим — отозвать: ./panel bot revoke" ;;
        bot_not_enabled) printf '%s' "бот не включён. Сначала: ./panel bot enable" ;;
        bot_key_replaced) printf '%s' "Ключ бота заменён: новый работает, старый отозван." ;;
        bot_key_not_seen) printf '%s' "Бот с новым ключом не вышел на связь, поэтому старый ключ НЕ отозван. Логи: ./panel logs bot" ;;
        bot_remote_need_terminal) printf '%s' "ключ можно показать только в терминале (иначе он осядет в логе): запустите команду в терминале." ;;
        bot_key_prompt) printf '%s' "Ключ бота для отзыва (ввод скрыт): " ;;
        bot_bad_key) printf '%s' "это не похоже на ключ бота (ожидается twb_… и ещё 43 символа)." ;;
        bot_revoke_failed) printf '%s' "не удалось отозвать ключ: такого ключа нет или панель недоступна." ;;
        bot_revoked) printf '%s' "Ключ отозван." ;;
        bot_usage) printf '%s' "Использование: ./panel bot enable | disable | key [--remote] | revoke | status | invite ARGS…" ;;
        bot_unknown_sub) printf '%s' "неизвестная подкоманда бота: %s (доступны: enable, disable, key, revoke, status, invite)" ;;
    esac
}

lib_msg_en() {
    case $1 in
        bot_choice_invalid) printf '%s' "--bot: allowed values are local, remote, none (got: \"%s\")." ;;
        bot_menu_title) printf '%s' "Telegram bot (people get their VPN themselves by invitation):" ;;
        bot_menu_local) printf '%s' "  1) on this server" ;;
        bot_menu_remote) printf '%s' "  2) on another server (I will print the install command)" ;;
        bot_menu_none) printf '%s' "  3) not needed (default; add it later: ./panel bot enable)" ;;
        bot_menu_choice) printf '%s' "Choice [3]: " ;;
        bot_menu_again) printf '%s' "Enter 1, 2 or 3." ;;
        bot_old_compose) printf '%s' "the bot needs Docker Compose 2.24 or newer (this one is %s)." ;;
        bot_no_token) printf '%s' "the bot token from @BotFather is needed: set the TELEGRAM_BOT_TOKEN variable or run in a terminal." ;;
        bot_bad_token) printf '%s' "this does not look like a bot token (expected 123456789:AAF... from @BotFather)." ;;
        bot_token_prompt) printf '%s' "Bot token from @BotFather (input hidden): " ;;
        bot_telegram_failed) printf '%s' "Telegram rejected the token or is unreachable from this server. If Telegram does not open from here, run the bot on another server: %s" ;;
        bot_key_failed) printf '%s' "could not issue the bot key. Retry once the panel is running: ./panel bot enable" ;;
        bot_env_written) printf '%s' "Bot settings are saved in %s (owner-only access)." ;;
        bot_already) printf '%s' "The bot is already set up (%s): the token and key are left as they are." ;;
        bot_missing_image) printf '%s' "the bot image is missing on this machine (%s). Bring it from another computer: ./deploy/ship-images.sh THIS_SERVER" ;;
        bot_starting) printf '%s' "Starting the bot..." ;;
        bot_start_failed) printf '%s' "could not start the bot. What happened: ./panel logs bot" ;;
        bot_waiting) printf '%s' "Waiting for the bot to reach the panel (up to %s s)..." ;;
        bot_running) printf '%s' "The bot is running and talks to the panel." ;;
        bot_silent) printf '%s' "The bot has not reached the panel yet. See what it says: ./panel logs bot" ;;
        bot_no_start) printf '%s' "The bot is not set up (--no-start). Once the panel is running: ./panel bot enable" ;;
        bot_remote_no_start) printf '%s' "No key was issued for the bot on another server (--no-start). Once the panel is running: ./panel bot key --remote" ;;
        bot_remote_no_terminal) printf '%s' "The bot key can only be shown in a terminal (otherwise it would end up in a log). Issue it there: ./panel bot key --remote" ;;
        bot_remote_title) printf '%s' "Bot on another server. Run there (Docker and git are needed):" ;;
        bot_remote_key_title) printf '%s' "The bot key - shown once, the installer asks for it with hidden input:" ;;
        bot_remote_token_note) printf '%s' "The Telegram token (from @BotFather) is asked for there as well." ;;
        bot_remote_repo_unknown) printf '%s' "<repository address>" ;;
        bot_remote_old_key) printf '%s' "If this replaces a key, revoke the old one after the bot is reconfigured: ./panel bot revoke" ;;
        bot_disabled) printf '%s' "The bot is stopped and removed from startup. Its settings (%s) are kept; the key stays valid - revoke it with: ./panel bot revoke" ;;
        bot_not_enabled) printf '%s' "the bot is not enabled. First: ./panel bot enable" ;;
        bot_key_replaced) printf '%s' "The bot key is replaced: the new one works, the old one is revoked." ;;
        bot_key_not_seen) printf '%s' "The bot did not reach the panel with the new key, so the old key was NOT revoked. Logs: ./panel logs bot" ;;
        bot_remote_need_terminal) printf '%s' "the key can only be shown in a terminal (otherwise it would end up in a log): run the command in a terminal." ;;
        bot_key_prompt) printf '%s' "Bot key to revoke (input hidden): " ;;
        bot_bad_key) printf '%s' "this does not look like a bot key (expected twb_... and 43 more characters)." ;;
        bot_revoke_failed) printf '%s' "could not revoke the key: there is no such key or the panel is unreachable." ;;
        bot_revoked) printf '%s' "The key is revoked." ;;
        bot_usage) printf '%s' "Usage: ./panel bot enable | disable | key [--remote] | revoke | status | invite ARGS..." ;;
        bot_unknown_sub) printf '%s' "unknown bot subcommand: %s (available: enable, disable, key, revoke, status, invite)" ;;
    esac
}
lib_register bot # сообщения доступны через t() под именем bot_msg_ru / bot_msg_en

# --- мелочи ---------------------------------------------------------------------------------------------------------

bot_terminal() { [ -t 0 ] && [ -t 1 ]; }

# Ошибка шага бота: печатается как die(), но выполнение продолжается (install.sh не должен падать из-за бота).
bot_fail() {
    if [ "$lang" = ru ]; then printf 'Ошибка: %s\n' "$*" >&2; else printf 'Error: %s\n' "$*" >&2; fi
    return 1
}

# Формат токена BotFather и ключа бота проверяется строго: значение попадает в конфиг curl (`url = "…"`), и лишний символ
# (кавычка, пробел, перевод строки) позволил бы дописать туда свои директивы.
bot_token_ok() { [[ $1 =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]]; }
bot_key_ok() { [[ $1 =~ ^twb_[A-Za-z0-9_-]{43}$ ]]; }

# Значение переменной из файла настроек бота ($1 — файл, $2 — имя).
bot_env_get() {
    local value
    value=$(grep -m1 "^$2=" "$1" 2> /dev/null | cut -d= -f2- || true)
    printf '%s' "$value"
}

# Версия Docker Compose не ниже 2.24 (там появился env_file с required: false). bot_compose_version — для сообщения.
bot_compose_version=""
bot_compose_ok() {
    local text major minor
    text=$(docker compose version 2> /dev/null) || return 1
    bot_compose_version=$(printf '%s' "$text" | sed -n 's/^[^0-9]*\([0-9][0-9.]*\).*/\1/p' | head -n 1)
    major=${bot_compose_version%%.*}
    minor=${bot_compose_version#*.}
    minor=${minor%%.*}
    [[ $major =~ ^[0-9]+$ ]] && [[ $minor =~ ^[0-9]+$ ]] || return 1
    [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 24 ]; }
}

# Сколько ждать бота: INSTALL_WAIT_SECONDS (как и для панели), но не дольше двух минут — бот поднимается быстро.
bot_wait_seconds() {
    local seconds=${INSTALL_WAIT_SECONDS:-120}
    [[ $seconds =~ ^[1-9][0-9]*$ ]] || seconds=120
    [ "$seconds" -le 120 ] || seconds=120
    printf '%s' "$seconds"
}

# Секрет из переменной окружения, а в терминале — скрытым вводом. Результат — в bot_secret.
bot_secret=""
bot_read_secret() {
    local name=$1 prompt=$2 value=${!1:-}
    if [ -z "$value" ] && bot_terminal; then
        read -r -s -p "$prompt" value || value=""
        echo
    fi
    bot_secret=$value
    [ -n "$value" ]
}

# --- Telegram и панель (curl, секреты — через stdin) -----------------------------------------------------------------

# Проверка токена: getMe. Печатает имя бота. Неверный токен и недоступная сеть — одинаково «нет».
bot_get_me() {
    local answer
    answer=$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$1" | curl -fsS --max-time 15 -K - 2> /dev/null) || return 1
    case $answer in
        *'"ok":true'*) ;;
        *) return 1 ;;
    esac
    printf '%s' "$answer" | sed -n 's/.*"username":"\([A-Za-z0-9_]*\)".*/\1/p' | head -n 1
}

# --- включить, выключить ---------------------------------------------------------------------------------------------

bot_profile_has() {
    local part parts
    IFS=, read -ra parts <<< "$(env_get COMPOSE_PROFILES)"
    for part in ${parts[@]+"${parts[@]}"}; do
        [ "$part" != bot ] || return 0
    done
    return 1
}

bot_profile_add() {
    bot_profile_has && return 0
    local current
    current=$(env_get COMPOSE_PROFILES)
    env_set COMPOSE_PROFILES "${current:+$current,}bot"
}

bot_profile_remove() {
    bot_profile_has || return 0
    local part parts kept=""
    IFS=, read -ra parts <<< "$(env_get COMPOSE_PROFILES)"
    for part in ${parts[@]+"${parts[@]}"}; do
        [ "$part" = bot ] || kept="${kept:+$kept,}$part"
    done
    env_set COMPOSE_PROFILES "$kept"
}

# Выпускает ключ командой на сервере панели (--raw: в stdout только ключ). Результат — в bot_new_key.
bot_new_key=""
bot_issue_key() {
    local out
    out=$("${compose[@]}" run --rm -T -e "PANEL_LANG=$lang" api python -m scripts.bot_key create --name "$BOT_KEY_NAME" --raw 2> /dev/null) || return 1
    bot_new_key=$(printf '%s\n' "$out" | tail -n 1 | tr -d '[:space:]')
    bot_key_ok "$bot_new_key"
}

# Записывает .env.bot целиком: временный файл рядом (права 0600 с самого создания) и переименование.
bot_write_env() {
    local tmp
    tmp=$(umask 077 && mktemp "$BOT_ENV_FILE.XXXXXX")
    {
        printf 'TELEGRAM_BOT_TOKEN=%s\n' "$1"
        printf 'BOT_API_KEY=%s\n' "$2"
    } > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$BOT_ENV_FILE"
}

# docker compose up бота. $@ — дополнительные ключи (--force-recreate).
bot_up() {
    local args=(up -d)
    if [ "${bot_build:-1}" -eq 1 ]; then
        args+=(--build)
    elif ! docker image inspect "$BOT_IMAGE" > /dev/null 2>&1; then
        bot_fail "$(t bot_missing_image "$BOT_IMAGE")"
        return 1
    fi
    args+=("$@" bot)
    say "$(t bot_starting)"
    "${compose[@]}" "${args[@]}" || { bot_fail "$(t bot_start_failed)"; return 1; }
}

# Ждёт, пока бот с ключом $1 выйдет на связь (heartbeat не старше BOT_SEEN_WITHIN секунд).
bot_wait_seen() {
    local key=$1 seconds deadline
    seconds=$(bot_wait_seconds)
    deadline=$((SECONDS + seconds))
    say "$(t bot_waiting "$seconds")"
    while :; do
        if printf '%s' "$key" | "${compose[@]}" run --rm -T -e "PANEL_LANG=$lang" api python -m scripts.bot_key seen --within "$BOT_SEEN_WITHIN" > /dev/null 2>&1; then
            return 0
        fi
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep 2
    done
}

# Включить бота на этом сервере: токен -> getMe -> ключ -> .env.bot -> профиль -> запуск -> ждём бота. Уже настроенный
# бот (есть .env.bot) только включается: токен и ключ не трогаем и не спрашиваем.
bot_enable() {
    bot_compose_ok || { bot_fail "$(t bot_old_compose "${bot_compose_version:-?}")"; return 1; }
    local token key
    if [ -f "$BOT_ENV_FILE" ]; then
        say "$(t bot_already "$BOT_ENV_FILE")"
        key=$(bot_env_get "$BOT_ENV_FILE" BOT_API_KEY)
    else
        bot_read_secret TELEGRAM_BOT_TOKEN "$(t bot_token_prompt)" || { bot_fail "$(t bot_no_token)"; return 1; }
        token=$bot_secret
        unset TELEGRAM_BOT_TOKEN # дальше токен нужен только в памяти скрипта: дочерним процессам его не передаём
        bot_token_ok "$token" || { bot_fail "$(t bot_bad_token)"; return 1; }
        bot_get_me "$token" > /dev/null || { bot_fail "$(t bot_telegram_failed "./panel bot key --remote")"; return 1; }
        bot_issue_key || { bot_fail "$(t bot_key_failed)"; return 1; }
        key=$bot_new_key
        bot_write_env "$token" "$key"
        say "$(t bot_env_written "$BOT_ENV_FILE")"
    fi
    bot_profile_add
    bot_up || return 1
    if bot_wait_seen "$key"; then say "$(t bot_running)"; else say "$(t bot_silent)"; fi
}

bot_disable() {
    "${compose[@]}" stop bot 2> /dev/null || true
    "${compose[@]}" rm -f bot 2> /dev/null || true
    bot_profile_remove
    say "$(t bot_disabled "$BOT_ENV_FILE")"
}

# Заменить ключ локального бота: новый записан и бот перезапущен — и только когда он вышел на связь, старый отзывается
# (иначе неудачная смена оставила бы бота без рабочего ключа).
bot_rotate_key() {
    [ -f "$BOT_ENV_FILE" ] || { bot_fail "$(t bot_not_enabled)"; return 1; }
    local old
    old=$(bot_env_get "$BOT_ENV_FILE" BOT_API_KEY)
    local bot_build=0 # пересоздаём контейнер, образ не пересобираем
    bot_issue_key || { bot_fail "$(t bot_key_failed)"; return 1; }
    (ENV_FILE=$BOT_ENV_FILE && env_set BOT_API_KEY "$bot_new_key")
    bot_up --force-recreate || return 1
    if bot_wait_seen "$bot_new_key"; then
        if [ -n "$old" ]; then
            printf '%s' "$old" | "${compose[@]}" run --rm -T -e "PANEL_LANG=$lang" api python -m scripts.bot_key revoke > /dev/null 2>&1 || true
        fi
        say "$(t bot_key_replaced)"
    else
        say "$(t bot_key_not_seen)"
        return 1
    fi
}

# --- бот на другом сервере -------------------------------------------------------------------------------------------

# Адрес репозитория для `git clone` на втором сервере: https, без учётных данных. TOLLWIRE_REPO_URL важнее origin.
bot_repo_url() {
    local url=${TOLLWIRE_REPO_URL:-} rest
    if [ -z "$url" ] && command -v git > /dev/null 2>&1; then
        url=$(git remote get-url origin 2> /dev/null || true)
    fi
    case $url in
        "") return 1 ;;
        git@*:*)
            rest=${url#git@}
            url="https://${rest%%:*}/${rest#*:}"
            ;;
        ssh://* | http://* | https://*)
            rest=${url#*://}
            case ${rest%%/*} in *@*) rest=${rest#*@} ;; esac # userinfo (логин:пароль@) — вон
            url="https://$rest"
            ;;
        *) return 1 ;;
    esac
    printf '%s' "$url"
}

# Команда для второго сервера и ключ: ключ выпускается только для показа человеку в терминале. $1 — адрес панели,
# $2 — отпечаток её сертификата (может быть пустым, если сертификат настоящий).
bot_remote() {
    local panel_url=$1 fingerprint=${2:-} repo command
    if ! bot_terminal; then
        say "$(t bot_remote_no_terminal)"
        return 0
    fi
    bot_issue_key || { bot_fail "$(t bot_key_failed)"; return 1; }
    repo=$(bot_repo_url || t bot_remote_repo_unknown)
    command="git clone $repo tollwire && cd tollwire && ./bot/install-bot.sh --panel-url $panel_url"
    [ -z "$fingerprint" ] || command="$command --panel-fingerprint $fingerprint"
    say ""
    say "$(t bot_remote_title)"
    say "  $command"
    say ""
    say "$(t bot_remote_key_title)"
    say "$bot_new_key"
    say ""
    say "$(t bot_remote_token_note)"
    say "$(t bot_remote_old_key)"
    bot_new_key=""
}

# Выпустить ключ для бота на другом сервере и показать его один раз (./panel bot key --remote).
bot_key_remote() {
    bot_terminal || { bot_fail "$(t bot_remote_need_terminal)"; return 1; }
    bot_issue_key || { bot_fail "$(t bot_key_failed)"; return 1; }
    say ""
    say "$(t bot_remote_key_title)"
    say "$bot_new_key"
    say ""
    say "$(t bot_remote_old_key)"
    bot_new_key=""
}

# Отозвать ключ: из stdin (конвейер) или скрытым вводом в терминале.
bot_revoke() {
    local key=""
    if bot_terminal; then
        read -r -s -p "$(t bot_key_prompt)" key || key=""
        echo
    else
        key=$(cat)
    fi
    key=${key//[[:space:]]/}
    bot_key_ok "$key" || { bot_fail "$(t bot_bad_key)"; return 1; }
    printf '%s' "$key" | "${compose[@]}" run --rm -T -e "PANEL_LANG=$lang" api python -m scripts.bot_key revoke > /dev/null 2>&1 \
        || { bot_fail "$(t bot_revoke_failed)"; return 1; }
    say "$(t bot_revoked)"
}

# Вопрос при свежей установке (install.sh в терминале). Ответ — в bot_choice.
bot_choice=""
bot_ask_choice() {
    local answer
    say ""
    say "$(t bot_menu_title)"
    say "$(t bot_menu_local)"
    say "$(t bot_menu_remote)"
    say "$(t bot_menu_none)"
    while true; do
        read -r -p "$(t bot_menu_choice)" answer || answer=3
        case ${answer:-3} in
            1) bot_choice=local; return 0 ;;
            2) bot_choice=remote; return 0 ;;
            3) bot_choice=none; return 0 ;;
            *) say "$(t bot_menu_again)" ;;
        esac
    done
}
