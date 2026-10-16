# Let's Encrypt для install.sh и ./panel: установка certbot, выпуск сертификата и его автообновление.
# Подключается командой `source deploy/letsencrypt.sh` после deploy/common.sh. Хозяин (install.sh или panel) задаёт
# ENV_FILE, COMPOSE_FILE, CERT_DIR, CERT_FILE и KEY_FILE; общие t, say, die, env_get и port_busy — из common.sh.
#
# Как это устроено. certbot работает в режиме --standalone: на время проверки он сам слушает порт 80. Поэтому nginx
# панели на эти секунды останавливается (pre-hook) и затем запускается снова (post-hook); новые файлы в certs/
# кладёт deploy-hook. Хуки лежат файлами в $TOLLWIRE_LE_DIR (по умолчанию /etc/letsencrypt/tollwire) и сохраняются
# в настройках обновления certbot: его системный таймер (certbot.timer) обновляет сертификат раз в ~2 месяца сам.
# Условия Let's Encrypt принимает человек: ответом на вопрос или флагом --accept-le-terms; здесь они не принимаются.

LE_CERT_NAME=tollwire
LE_DIR=${TOLLWIRE_LE_DIR:-/etc/letsencrypt/tollwire}
LE_HTTP_PORT=${TOLLWIRE_LE_HTTP_PORT:-80} # порт проверки домена (80 — требование Let's Encrypt; менять только в тестах)
LE_TERMS_URL=https://letsencrypt.org/repository/
le_accept=0   # принял ли человек условия Let's Encrypt (флаг --accept-le-terms или ответ в меню)
le_email=""   # адрес для уведомлений Let's Encrypt; пусто — без адреса
le_sudo=""    # "" (мы root) или "sudo"

lib_msg_ru() {
    case $1 in
        le_bad_domain) printf '%s' "для Let's Encrypt нужен настоящий домен (не IP-адрес и не localhost): сейчас «%s»." ;;
        le_need_terms) printf '%s' "Let's Encrypt без терминала требует явного согласия с условиями: добавьте --accept-le-terms (условия: %s)." ;;
        le_bad_email) printf '%s' "некорректный email для Let's Encrypt: %s" ;;
        le_plan) printf '%s' "Что произойдёт:" ;;
        le_plan_certbot) printf '%s' "  - при необходимости будет установлен certbot (пакетным менеджером, нужен sudo);" ;;
        le_plan_nginx) printf '%s' "  - на несколько секунд остановится nginx панели: certbot проверит домен на порту 80;" ;;
        le_plan_terms) printf '%s' "  - вы принимаете условия Let's Encrypt (Subscriber Agreement): %s" ;;
        le_plan_ports) printf '%s' "  - порт 80 этого сервера должен быть доступен из интернета (проброс на роутере)." ;;
        le_accept_prompt) printf '%s' "Принять условия Let's Encrypt и продолжить? [y/N]: " ;;
        le_email_prompt) printf '%s' "Email для уведомлений Let's Encrypt (пусто — без адреса): " ;;
        le_declined) printf '%s' "Согласие не получено — Let's Encrypt пропущен." ;;
        le_no_root) printf '%s' "нужны права root: запустите от root или установите sudo." ;;
        le_pkg_unsupported) printf '%s' "не умею ставить certbot в этой системе. Установите его сами (https://certbot.eff.org) и повторите." ;;
        le_installing) printf '%s' "Ставлю certbot…" ;;
        le_install_failed) printf '%s' "не удалось установить certbot." ;;
        le_port_busy) printf '%s' "порт 80 занят не панелью — certbot не сможет проверить домен. Освободите порт 80." ;;
        le_dry_run) printf '%s' "Проверяю домен %s на тестовом сервере Let's Encrypt (сертификат пока не выпускается)…" ;;
        le_dry_failed) printf '%s' "проверка домена не прошла. Частые причины: порт 80 не доступен из интернета (проброс на роутере, блокировка провайдером) или домен указывает на другой адрес. Подробности: /var/log/letsencrypt/letsencrypt.log" ;;
        le_issuing) printf '%s' "Выпускаю сертификат для %s…" ;;
        le_issue_failed) printf '%s' "выпуск сертификата не удался. Подробности: /var/log/letsencrypt/letsencrypt.log" ;;
        le_not_applied) printf '%s' "certbot отработал, но сертификат в %s/ не заменился. Смотрите /var/log/letsencrypt/letsencrypt.log" ;;
        le_done) printf '%s' "Сертификат Let's Encrypt установлен в %s/ (до: %s)." ;;
        le_renewal) printf '%s' "Обновляться он будет сам: certbot проверяет срок раз в сутки, на несколько секунд останавливает nginx и подставляет новые файлы." ;;
        le_no_timer) printf '%s' "Внимание: системный таймер автообновления certbot не найден. Запускайте 'sudo certbot renew' по расписанию (cron)." ;;
    esac
}

lib_msg_en() {
    case $1 in
        le_bad_domain) printf '%s' "Let's Encrypt needs a real domain name (not an IP address or localhost): got \"%s\"." ;;
        le_need_terms) printf '%s' "Let's Encrypt without a terminal needs explicit consent to its terms: add --accept-le-terms (terms: %s)." ;;
        le_bad_email) printf '%s' "invalid email for Let's Encrypt: %s" ;;
        le_plan) printf '%s' "What will happen:" ;;
        le_plan_certbot) printf '%s' "  - certbot will be installed if missing (with the package manager, needs sudo);" ;;
        le_plan_nginx) printf '%s' "  - the panel's nginx stops for a few seconds: certbot verifies the domain on port 80;" ;;
        le_plan_terms) printf '%s' "  - you accept the Let's Encrypt Subscriber Agreement: %s" ;;
        le_plan_ports) printf '%s' "  - port 80 of this server must be reachable from the internet (router port forwarding)." ;;
        le_accept_prompt) printf '%s' "Accept the Let's Encrypt terms and continue? [y/N]: " ;;
        le_email_prompt) printf '%s' "Email for Let's Encrypt notices (empty - no address): " ;;
        le_declined) printf '%s' "No consent given - Let's Encrypt skipped." ;;
        le_no_root) printf '%s' "root permissions are needed: run as root or install sudo." ;;
        le_pkg_unsupported) printf '%s' "cannot install certbot on this system. Install it yourself (https://certbot.eff.org) and retry." ;;
        le_installing) printf '%s' "Installing certbot..." ;;
        le_install_failed) printf '%s' "could not install certbot." ;;
        le_port_busy) printf '%s' "port 80 is used by something other than the panel - certbot cannot verify the domain. Free port 80." ;;
        le_dry_run) printf '%s' "Checking the domain %s on Let's Encrypt's test server (no certificate is issued yet)..." ;;
        le_dry_failed) printf '%s' "the domain check failed. Common causes: port 80 is not reachable from the internet (router forwarding, ISP blocking) or the domain points elsewhere. Details: /var/log/letsencrypt/letsencrypt.log" ;;
        le_issuing) printf '%s' "Issuing the certificate for %s..." ;;
        le_issue_failed) printf '%s' "issuing the certificate failed. Details: /var/log/letsencrypt/letsencrypt.log" ;;
        le_not_applied) printf '%s' "certbot finished but the certificate in %s/ was not replaced. See /var/log/letsencrypt/letsencrypt.log" ;;
        le_done) printf '%s' "Let's Encrypt certificate installed in %s/ (valid until: %s)." ;;
        le_renewal) printf '%s' "It renews itself: certbot checks the expiry daily, stops nginx for a few seconds and puts the new files in place." ;;
        le_no_timer) printf '%s' "Warning: the certbot auto-renewal timer was not found. Run 'sudo certbot renew' on a schedule (cron)." ;;
    esac
}

# Настоящее доменное имя: есть точка, не IP, не служебная зона. Let's Encrypt не выдаёт сертификаты на IP и localhost.
le_public_domain() {
    local d=$1
    [[ $d =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] || return 1
    [[ $d =~ ^[0-9.]+$ ]] && return 1
    case $d in
        localhost | *.localhost | *.local | *.lan | *.home | *.internal | *.test | *.example | *.invalid | *.localdomain) return 1 ;;
    esac
    return 0
}

le_valid_email() { [[ $1 =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; }

# Согласие человека: флагом (le_accept=1) или ответом в терминале. 0 — согласен; 1 — нет (сообщение уже показано).
le_consent() {
    if [ "$le_accept" -ne 1 ]; then
        if ! { [ -t 0 ] && [ -t 1 ]; }; then
            printf 'Error: %s\n' "$(t le_need_terms "$LE_TERMS_URL")" >&2
            return 1
        fi
        say ""
        say "$(t le_plan)"
        say "$(t le_plan_certbot)"
        say "$(t le_plan_nginx)"
        say "$(t le_plan_terms "$LE_TERMS_URL")"
        say "$(t le_plan_ports)"
        local answer=""
        read -r -p "$(t le_accept_prompt)" answer || answer=""
        case $answer in
            y | Y | yes | YES | д | Д | да | Да) ;;
            *)
                say "$(t le_declined)"
                return 1
                ;;
        esac
        le_accept=1
        if [ -z "$le_email" ]; then
            read -r -p "$(t le_email_prompt)" le_email || le_email=""
        fi
    fi
    if [ -n "$le_email" ] && ! le_valid_email "$le_email"; then
        printf 'Error: %s\n' "$(t le_bad_email "$le_email")" >&2
        return 1
    fi
    return 0
}

le_fail() {
    printf '%s\n' "$(t "$@")" >&2
    return 1
}

le_prepare_root() {
    if [ "$(id -u)" -eq 0 ]; then
        le_sudo=""
    elif command -v sudo > /dev/null 2>&1; then
        le_sudo="sudo"
    else
        le_fail le_no_root
    fi
}

le_ensure_certbot() {
    command -v certbot > /dev/null 2>&1 && return 0
    say "$(t le_installing)"
    if command -v apt-get > /dev/null 2>&1; then
        # shellcheck disable=SC2086  # le_sudo — пусто или одно слово
        $le_sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y certbot > /dev/null 2>&1 \
            || { $le_sudo apt-get update -qq > /dev/null 2>&1 && $le_sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y certbot > /dev/null 2>&1; } \
            || { le_fail le_install_failed; return 1; }
    elif command -v dnf > /dev/null 2>&1; then
        # shellcheck disable=SC2086
        $le_sudo dnf install -y certbot > /dev/null 2>&1 || { le_fail le_install_failed; return 1; }
    else
        le_fail le_pkg_unsupported
        return 1
    fi
    command -v certbot > /dev/null 2>&1 || { le_fail le_install_failed; return 1; }
}

# Скрипты-хуки для certbot. Пути зашиты при создании (printf %q): хуки работают от root и из любой папки.
le_write_hooks() {
    local project compose_cmd work
    project=$(pwd)
    [[ $LE_DIR != *[[:space:]]* ]] || return 1
    compose_cmd=$(printf 'docker compose --env-file %q -f %q' "$project/$ENV_FILE" "$project/$COMPOSE_FILE")
    work=$(mktemp -d)
    # pre: освободить порт 80, запомнив, что nginx работал
    cat > "$work/pre.sh" <<EOF
#!/usr/bin/env bash
# Tollwire: освободить порт 80 для проверки домена (certbot --standalone).
if [ -n "\$($compose_cmd ps -q nginx 2> /dev/null)" ]; then
    : > $(printf '%q' "$LE_DIR/nginx-was-running")
    $compose_cmd stop nginx > /dev/null 2>&1
fi
exit 0
EOF
    # post: вернуть nginx, если мы его останавливали
    cat > "$work/post.sh" <<EOF
#!/usr/bin/env bash
# Tollwire: запустить nginx обратно (если pre-hook его останавливал).
if [ -e $(printf '%q' "$LE_DIR/nginx-was-running") ]; then
    rm -f $(printf '%q' "$LE_DIR/nginx-was-running")
    $compose_cmd start nginx > /dev/null 2>&1
fi
exit 0
EOF
    # deploy: положить новые файлы в certs/ (целиком или никак), владелец — как у папки certs/
    cat > "$work/deploy.sh" <<EOF
#!/usr/bin/env bash
# Tollwire: подставить выпущенный или обновлённый сертификат в certs/ панели.
set -eu
live=\${RENEWED_LINEAGE:-${LE_LIVE_DIR:-/etc/letsencrypt/live}/$LE_CERT_NAME}
dest=$(printf '%q' "$project/$CERT_DIR")
tmp=\$(mktemp -d "\$dest/.le.XXXXXX")
trap 'rm -rf "\$tmp"' EXIT
cp "\$live/fullchain.pem" "\$tmp/fullchain.pem"
cp "\$live/privkey.pem" "\$tmp/privkey.pem"
chmod 644 "\$tmp/fullchain.pem"
chmod 600 "\$tmp/privkey.pem"
chown --reference="\$dest" "\$tmp/fullchain.pem" "\$tmp/privkey.pem" 2> /dev/null || true
mv "\$tmp/fullchain.pem" "\$dest/fullchain.pem"
mv "\$tmp/privkey.pem" "\$dest/privkey.pem"
EOF
    # shellcheck disable=SC2086
    $le_sudo mkdir -p "$LE_DIR" \
        && $le_sudo install -m 755 "$work/pre.sh" "$LE_DIR/pre.sh" \
        && $le_sudo install -m 755 "$work/post.sh" "$LE_DIR/post.sh" \
        && $le_sudo install -m 755 "$work/deploy.sh" "$LE_DIR/deploy.sh"
    local status=$?
    rm -rf "$work"
    return $status
}

# Выпуск сертификата для $1. Предполагает, что согласие уже получено (le_consent). 0 — готово; 1 — не вышло
# (сообщение показано, certs/ не тронуты: файлы подменяет только deploy-hook после успешного выпуска).
le_issue() {
    local domain=$1 args=() before after
    le_public_domain "$domain" || { le_fail le_bad_domain "$domain"; return 1; }
    le_prepare_root || return 1
    le_ensure_certbot || return 1
    # Порт 80 должен быть свободен или занят самой панелью (её nginx хук остановит сам).
    if [ -z "$("${compose[@]}" ps -q nginx 2> /dev/null)" ] && port_busy "$LE_HTTP_PORT"; then
        le_fail le_port_busy
        return 1
    fi
    le_write_hooks || { le_fail le_install_failed; return 1; }

    args=(certonly --standalone --preferred-challenges http --non-interactive --agree-tos
        --cert-name "$LE_CERT_NAME" -d "$domain"
        --pre-hook "$LE_DIR/pre.sh" --post-hook "$LE_DIR/post.sh" --deploy-hook "$LE_DIR/deploy.sh")
    if [ -n "$le_email" ]; then
        args+=(--email "$le_email" --no-eff-email)
    else
        args+=(--register-unsafely-without-email)
    fi

    say "$(t le_dry_run "$domain")"
    # shellcheck disable=SC2086
    $le_sudo certbot "${args[@]}" --dry-run > /dev/null 2>&1 || { le_fail le_dry_failed; return 1; }

    say "$(t le_issuing "$domain")"
    before=$(openssl x509 -in "$CERT_FILE" -noout -fingerprint -sha256 2> /dev/null || true)
    # shellcheck disable=SC2086
    $le_sudo certbot "${args[@]}" > /dev/null 2>&1 || { le_fail le_issue_failed; return 1; }
    after=$(openssl x509 -in "$CERT_FILE" -noout -fingerprint -sha256 2> /dev/null || true)
    if [ -z "$after" ] || [ "$before" = "$after" ]; then
        le_fail le_not_applied "$CERT_DIR"
        return 1
    fi
    return 0
}

# Сообщение об успехе и проверка, что автообновление настроено (системный таймер certbot).
le_report_success() {
    local expiry
    expiry=$(openssl x509 -in "$CERT_FILE" -noout -enddate | cut -d= -f2)
    say "$(t le_done "$CERT_DIR" "$expiry")"
    say "$(t le_renewal)"
    if command -v systemctl > /dev/null 2>&1 && ! systemctl is-enabled certbot.timer > /dev/null 2>&1; then
        say "$(t le_no_timer)"
    fi
}
