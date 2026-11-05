#!/usr/bin/env bash
# Isolated production-image checks. No production env files, ports or VPN targets.
set -euo pipefail
[ "$#" -eq 3 ] || { echo 'Usage: smoke-images.sh APP_IMAGE WEB_IMAGE BOT_IMAGE' >&2; exit 2; }
app=$1 web=$2 bot=$3
prefix="tollwire-smoke-$(date +%s)-$$"
work=$(mktemp -d)
network="$prefix"
cleanup() {
    docker rm -fv "$prefix-worker" "$prefix-api" "$prefix-web" "$prefix-config" "$prefix-pg" "$prefix-redis" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Test the shipped production proxy config, including TLS paths, with ephemeral
# certificates. Stream files over stdin so this also works with Docker Desktop.
mkdir -p "$work/conf.d" "$work/certs"
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cp "$repo/deploy/nginx/default.conf" "$work/conf.d/default.conf"
certificate_dir="$work/certs"
if command -v cygpath >/dev/null 2>&1; then certificate_dir=$(cygpath -m "$certificate_dir"); fi
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
    -keyout "$certificate_dir/privkey.pem" -out "$certificate_dir/fullchain.pem" >"$work/cert.log" 2>&1 \
    || { echo 'FAIL: ephemeral smoke certificate generation' >&2; exit 1; }
docker create --name "$prefix-config" --network none "$web" nginx -t >/dev/null
tar -cf - -C "$work" conf.d certs | docker cp - "$prefix-config:/etc/nginx"
docker start -a "$prefix-config" >"$work/nginx.log" 2>&1
[ "$(docker inspect -f '{{.State.ExitCode}}' "$prefix-config")" = 0 ] \
    || { echo 'FAIL: production nginx configuration' >&2; cat "$work/nginx.log" >&2; exit 1; }

# Synthetic test-only settings. All containers use a new internal-only network.
settings=(
    -e "DATABASE_URL=postgresql+asyncpg://smoke:smoke@${prefix}-pg:5432/smoke_test"
    -e "REDIS_URL=redis://${prefix}-redis:6379/0"
    -e 'JWT_SECRET_KEY=smoke-only-jwt-secret-0123456789abcdef'
    -e 'ENCRYPTION_KEY=ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg='
    -e JWT_ACCESS_EXPIRE=900 -e JWT_REFRESH_EXPIRE=86400
    -e SSH_CONNECT_TIMEOUT=5 -e SSH_COMMAND_TIMEOUT=5
    -e SCHEDULER_ENABLED=false -e LOG_FORMAT=json
)
docker run --rm --network none "${settings[@]}" "$app" \
    python -c 'import main, worker; main.app.openapi()' >"$work/import.log" 2>&1 \
    || { echo 'FAIL: production API/worker imports (runtime dependencies)' >&2; cat "$work/import.log" >&2; exit 1; }
docker run --rm --network none "$bot" python -c 'import tollwire_bot.__main__' >/dev/null
docker network create --internal "$network" >/dev/null
docker run -d --name "$prefix-pg" --network "$network" \
    -e POSTGRES_USER=smoke -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=smoke_test \
    postgres:18-alpine@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873 >/dev/null
docker run -d --name "$prefix-redis" --network "$network" \
    redis:8-alpine@sha256:3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0 >/dev/null
ready=0
for _ in $(seq 1 60); do
    if docker exec "$prefix-pg" pg_isready -U smoke -d smoke_test >/dev/null 2>&1 \
        && docker exec "$prefix-redis" redis-cli ping >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
done
[ "$ready" -eq 1 ] || { echo 'FAIL: isolated PostgreSQL/Redis readiness' >&2; exit 1; }
docker run --rm --network "$network" "${settings[@]}" "$app" alembic upgrade head >"$work/migrate.log" 2>&1 \
    || { echo 'FAIL: production migration on empty test database' >&2; cat "$work/migrate.log" >&2; exit 1; }
docker run -d --name "$prefix-api" --network "$network" "${settings[@]}" "$app" >/dev/null
ready=0
for _ in $(seq 1 60); do
    if docker exec "$prefix-api" python -c \
        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)' >/dev/null 2>&1; then
        ready=1; break
    fi
    sleep 1
done
[ "$ready" -eq 1 ] || { echo 'FAIL: isolated production API health' >&2; exit 1; }
docker run -d --name "$prefix-worker" --network "$network" "${settings[@]}" "$app" python src/worker.py >/dev/null
sleep 5
[ "$(docker inspect -f '{{.State.Running}}' "$prefix-worker")" = true ] \
    || { echo 'FAIL: isolated production worker exited' >&2; exit 1; }
docker logs "$prefix-worker" 2>&1 | grep -q 'worker started' \
    || { echo 'FAIL: worker startup was not observed' >&2; exit 1; }
docker run -d --name "$prefix-web" --network none "$web" >/dev/null
ready=0
for _ in $(seq 1 30); do
    if docker exec "$prefix-web" wget -q -O /tmp/index.html http://127.0.0.1/; then ready=1; break; fi
    sleep 1
done
[ "$ready" -eq 1 ] || { echo 'FAIL: production web HTTP' >&2; exit 1; }
docker exec "$prefix-web" sh -c \
    'test -s /tmp/index.html && find /usr/share/nginx/html/assets -type f | grep -q .'
echo 'PASS: production nginx config, imports, migration, API health, worker startup, bot import and web assets'
