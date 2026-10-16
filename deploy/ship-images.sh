#!/usr/bin/env bash
# Доставка образов панели на сервер, который не может сам собрать или скачать их (например, провайдер режет
# соединения к CDN Docker и npm). Запускать на машине с нормальным интернетом и Docker, из корня репозитория:
#
#   ./deploy/ship-images.sh homelab            # homelab — имя из ~/.ssh/config или user@host
#
# Образы собираются из закоммиченного состояния (git HEAD), по SSH уходят на сервер и получают там имена
# vpn-panel:latest и vpn-panel-web:latest. Дальше на сервере: ./panel update --no-pull --no-build
# (или ./install.sh --no-build при первой установке). Базовые образы postgres и redis сервер тянет сам.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# shellcheck source=deploy/common.sh
source deploy/common.sh

msg_ru() {
    case $1 in
        usage) printf '%s' "Использование: ./deploy/ship-images.sh СЕРВЕР   (имя из ~/.ssh/config или user@host)" ;;
        bad_host) printf '%s' "некорректное имя сервера: %s" ;;
        no_tool) printf '%s' "не найдено: %s" ;;
        not_git) printf '%s' "запускайте из корня git-репозитория." ;;
        dirty) printf '%s' "Внимание: образы собираются из коммита %s, локальные незакоммиченные правки в них не попадут." ;;
        building) printf '%s' "Собираю образы из коммита %s…" ;;
        build_failed) printf '%s' "сборка образа не удалась (%s)." ;;
        shipping) printf '%s' "Отправляю образы на %s (сжатый поток по SSH)…" ;;
        ship_failed) printf '%s' "не удалось передать образы на %s: проверьте вход по SSH-ключу и место на диске." ;;
        done) printf '%s' "Готово: на %s теперь vpn-panel:latest и vpn-panel-web:latest (коммит %s)." ;;
        next) printf '%s' "Дальше на сервере: ./panel update --no-pull --no-build   (первая установка: ./install.sh --no-build)" ;;
    esac
}

msg_en() {
    case $1 in
        usage) printf '%s' "Usage: ./deploy/ship-images.sh SERVER   (a name from ~/.ssh/config or user@host)" ;;
        bad_host) printf '%s' "invalid server name: %s" ;;
        no_tool) printf '%s' "not found: %s" ;;
        not_git) printf '%s' "run it from the root of the git repository." ;;
        dirty) printf '%s' "Warning: the images are built from commit %s; uncommitted local edits are not included." ;;
        building) printf '%s' "Building the images from commit %s..." ;;
        build_failed) printf '%s' "building the image failed (%s)." ;;
        shipping) printf '%s' "Sending the images to %s (a compressed stream over SSH)..." ;;
        ship_failed) printf '%s' "could not transfer the images to %s: check the SSH key login and the free disk space." ;;
        done) printf '%s' "Done: %s now has vpn-panel:latest and vpn-panel-web:latest (commit %s)." ;;
        next) printf '%s' "Next, on the server: ./panel update --no-pull --no-build   (first install: ./install.sh --no-build)" ;;
    esac
}

[ $# -eq 1 ] || { say "$(t usage)" >&2; exit 1; }
host=$1
# Имя уходит в команду ssh: только то, что бывает в именах хостов и user@host (в том числе не начинается с «-»).
[[ $host =~ ^[A-Za-z0-9_][A-Za-z0-9._@:-]*$ ]] || die "$(t bad_host "$host")"

for tool in git docker ssh gzip tar; do
    command -v "$tool" > /dev/null 2>&1 || die "$(t no_tool "$tool")"
done
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || die "$(t not_git)"

rev=$(git rev-parse --short=12 HEAD)
[ -z "$(git status --porcelain --untracked-files=no)" ] || say "$(t dirty "$rev")"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git archive HEAD | tar -x -C "$work"

# Локальные имена отдельные: не затираем vpn-panel:latest, который мог быть у запущенного здесь стека.
app="tollwire-ship/app:$rev"
web="tollwire-ship/web:$rev"

say "$(t building "$rev")"
docker build -q -t "$app" "$work" > /dev/null || die "$(t build_failed app)"
docker build -q -t "$web" "$work/frontend" > /dev/null || die "$(t build_failed web)"

say "$(t shipping "$host")"
# Образы собраны для архитектуры этой машины; на сервере должна быть та же (обычно x86-64).
docker save "$app" "$web" | gzip -1 | ssh "$host" "gunzip | docker load > /dev/null && docker tag $app vpn-panel:latest && docker tag $web vpn-panel-web:latest" \
    || die "$(t ship_failed "$host")"

say "$(t done "$host" "$rev")"
say "$(t next)"
