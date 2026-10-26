# Образ Telegram-бота. Контекст сборки - каталог bot/ (из корня репозитория бот ничего не берёт: он не импортирует src/).
# Базовые образы закреплены по дайджесту, как в корневом Dockerfile: сборка воспроизводима.

# --- сборка: зависимости ставит uv строго по uv.lock ----------------------------------------------------------------
FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# --- запуск: без uv и компилятора, только Python, .venv и код -------------------------------------------------------
FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4

RUN groupadd --system --gid 999 bot \
 && useradd --system --gid 999 --uid 999 --no-create-home bot \
 && mkdir /data && chown 999:999 /data

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DIR=/data

USER bot
# /data - именованный том: состояние диалогов (имя и почта до конца диалога) переживает пересоздание контейнера.
VOLUME ["/data"]

CMD ["python", "-m", "tollwire_bot"]
