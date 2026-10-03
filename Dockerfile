# Единый образ приложения: из него запускаются api, worker и migrate (меняется только команда в compose).

# --- сборка: зависимости ставит uv строго по uv.lock ----------------------------------------------------------------
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /bin/uv

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
FROM python:3.14-slim

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
