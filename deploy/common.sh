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

t() {
    local key=$1 fmt
    shift
    if [ "$lang" = ru ]; then fmt=$(msg_ru "$key"); else fmt=$(msg_en "$key"); fi
    # shellcheck disable=SC2059  # формат — наша собственная таблица выше
    printf -- "$fmt" "$@"
}

say() { printf '%s\n' "$*"; }
die() {
    if [ "$lang" = ru ]; then printf 'Ошибка: %s\n' "$*" >&2; else printf 'Error: %s\n' "$*" >&2; fi
    exit 1
}

