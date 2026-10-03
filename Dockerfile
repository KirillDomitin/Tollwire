# Единый образ приложения: из него запускаются api, worker и migrate (меняется только команда в compose).
# Базовые образы закреплены по дайджесту (тег@sha256): сборка воспроизводима, а подмена тега в registry не
# попадёт в наш образ. Обновление — осознанно: docker buildx imagetools inspect <образ:тег> → новый дайджест.

# --- сборка: зависимости ставит uv строго по uv.lock ----------------------------------------------------------------
FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Сначала только зависимости: этот слой пересобирается лишь при смене pyproject.toml/uv.lock, а не кода.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# --- запуск: без uv и без компилятора, только Python, .venv и код -------------------------------------------------
FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4

RUN groupadd --system --gid 999 app \
 && useradd --system --gid 999 --uid 999 --no-create-home app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

# По умолчанию — API. Воркер и миграции переопределяют команду в compose.
# --proxy-headers: IP клиента и схему берём из X-Forwarded-*, но только от FORWARDED_ALLOW_IPS (читает сам uvicorn).
# --no-access-log: строку на каждый запрос (с request_id) пишет RequestContextMiddleware.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header", "--no-access-log"]
