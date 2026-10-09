# CLAUDE.md — VPN Control Panel

Платформа с REST API и веб-интерфейсом для управления VPN-серверами на базе AmneziaVPN (протокол AmneziaWG):
серверы, VPN-пользователи, подписки и доступы, выдача конфигов, установка VPN по SSH, фоновые задачи,
мониторинг, аудит.

Здесь — только общее. Подробности по каждой части лежат рядом с кодом в своих `CLAUDE.md` (раздел 4) —
Claude Code подгружает их сам, когда работает с файлами этой папки. Установка и запуск — [`INSTALL.md`](INSTALL.md).

---

## 1. Как работать

- Код пишет Claude; пользователь принимает архитектурные решения. Архитектурную развилку — вынести на решение
  **до** написания кода, с рекомендацией.
- После изменений проверять: `ruff`, `mypy`, тесты, миграции (`upgrade`/`downgrade`), и живой прогон
  (HTTP-запросы, при необходимости — реальный сервер). Линтеры логику не ловят.
- Действия на реальных серверах, которые что-то меняют (установка/удаление, правка конфигов), — только с явного
  согласия пользователя.
- Поменялись устройство, правила или грабли модуля — обновить его `CLAUDE.md`.
- Коммиты — без подписи Co-Authored-By.
- Для актуальной документации библиотек — Context7 MCP.

---

## 2. Стек

- **Бэкенд:** Python 3.14, FastAPI, Pydantic v2 + pydantic-settings, PostgreSQL + SQLAlchemy 2.x (async, asyncpg)
  + Alembic, Redis (очередь, блокировки, лимиты, дедуп планировщика), AsyncSSH. Всё асинхронно.
- **Фронтенд:** React + Vite + TypeScript (strict), TanStack Query, openapi-fetch, uPlot — см. [`frontend/CLAUDE.md`](frontend/CLAUDE.md).
- **Инфраструктура:** Docker Compose (dev: postgres + redis; prod: api/worker/migrate/postgres/redis/nginx).
- **Качество:** pytest + httpx, Ruff, mypy; фронт — vitest, eslint, tsc.
- **Зависимости:** uv (`uv.lock`), npm (`frontend/package-lock.json`).

---

## 3. Архитектурные правила (нарушать нельзя)

```
API (routers)  →  Service (application)  →  Domain  →  Infrastructure  →  Persistence
```

- **Бизнес-логики в роутерах нет.** Роутер: HTTP, валидация входа (Pydantic-схемы), авторизация
  (`require(Permission.X)`), вызов сервиса, ответ.
- **Работа с VPN — только через `VPNProvider`.** Бизнес-логика не знает SSH-команд, Docker, путей к конфигам,
  способа генерации ключей.

  ```
  Business Logic → VPNProvider (интерфейс) → AmneziaWGProvider → SSHClient → SSH/Docker/Linux
  ```
- **`infrastructure/` не импортирует `modules/`** (кроме воркера и планировщика — они composition root). Поэтому
  фабрика провайдеров по `ProtocolType` живёт в `modules/servers/service.py`.
- **Работа с БД — через репозитории.** Сервис не пишет сырые запросы вперемешку с логикой.
- **Доменные ошибки** — классы из `core/exceptions.py`; в HTTP-коды их переводят глобальные обработчики в
  `main.py`, не роутеры.
- `Subscription` + `AccessGrant` — точка расширения под будущие тарифы и оплату.

---

## 4. Карта проекта

```
src/
  main.py                  сборка FastAPI: роутеры, обработчики ошибок, middleware, lifespan
  worker.py                точка входа процесса воркера (задачи + планировщик)
  scripts/                 create_admin, export_openapi
  core/                    → CLAUDE.md   конфиг, БД, Redis, безопасность, логи, лимиты, ошибки
  infrastructure/
    ssh/                   → CLAUDE.md   SSHClient, ключи хостов
    vpn/                   → CLAUDE.md   VPNProvider, AmneziaWG, awg0.conf, vpn://
    workers/               → CLAUDE.md   JobWorker, планировщик, ServerLock
    monitoring/            разбор замеров (описан в modules/monitoring/CLAUDE.md)
  modules/
    auth/                  → CLAUDE.md   админы, вход, JWT, сессии, RBAC
    servers/               → CLAUDE.md   VPNServer, SSH-учётка, ключ хоста, install, sync, health
    protocols/             → CLAUDE.md   VPNProtocol
    vpn_users/             → CLAUDE.md   VPNUser
    access/                → CLAUDE.md   Subscription, AccessGrant, VPNCredential (выдача конфига)
    jobs/                  → CLAUDE.md   Job, очередь на Redis
    audit/                 → CLAUDE.md   AuditLog
    monitoring/            → CLAUDE.md   метрики серверов
alembic/                   → CLAUDE.md   миграции и грабли Postgres enum
tests/                     → CLAUDE.md   как устроены тесты, фейковый SSH
frontend/                  → CLAUDE.md   React-интерфейс
```

Внутри модуля обычно: `models/` (ORM), `schemas.py` (Pydantic), `repository.py`, `service.py`, `api/v1/router.py`.

---

## 5. Безопасность (держать в голове всегда)

- Пароли админов — только Argon2id hash.
- Refresh-токены в БД — только hash; в браузер — HttpOnly cookie. Access-токен во фронте — только в памяти.
- SSH- и VPN-секреты — в БД только в зашифрованном виде (Fernet); `ENCRYPTION_KEY` — только из ENV, никогда не в
  PostgreSQL и не в Git.
- Никакого пользовательского ввода в SSH-команды — только константы и значения из белого списка; файлы — через
  SFTP. SQL — только через SQLAlchemy.
- В логи не попадают: пароли, приватные ключи (SSH/VPN), refresh-токены, JWT, VPN-конфиги, stdout SSH-команд.
- SSH: connect и command timeout обязательны; пароль SSH отправляется только серверу с закреплённым ключом хоста.
- Критические операции идемпотентны.
- Каждый эндпоинт требует ровно одно право (`require(Permission.X)`), матрица — в `modules/auth/CLAUDE.md`.
- `.env` / `.env.prod` не читать и не выводить — там реальные ключи.

---

## 6. Известные проблемы

- Сверка (sync) удаляет с сервера всех «сирот» `grant-*`. Если один сервер подключён к двум панелям (dev и prod),
  каждая удаляет клиентов другой — dev-воркер в таком случае не запускать.
- Health при незакреплённом ключе хоста пишет `down`, хотя сервер жив.
- Контейнер `amnezia-awg` запускается без `--restart` — после перезагрузки сервера VPN сам не поднимется.
- Плавающий тест `test_login_limited_by_ip`.
- `frontend/src/lib/audit.ts` (словарь событий аудита для интерфейса) не закоммичен; страниц аудита, задач и
  админов во фронтенде пока нет.

---

## 7. Команды

```bash
uv sync                                                 # зависимости
uv run uvicorn main:app --app-dir src --reload          # API (dev); без --app-dir: ModuleNotFoundError: core
uv run python src/worker.py                             # воркер (задачи + планировщик)
uv run ruff check . && uv run ruff format .             # линт и формат
uv run mypy src                                         # типы
uv run pytest                                           # тесты (нужен docker compose up: БД vpn_panel_test, Redis db 15)
uv run alembic upgrade head                             # миграции
PYTHONPATH=src uv run python -m scripts.create_admin you@example.com   # создать админа (пароль спросит скрыто)

# Фронтенд (frontend/, Node 24): API на :8000 + Vite на :5173 (проксирует /api)
PYTHONPATH=src uv run python -m scripts.export_openapi frontend/openapi.json   # схема API → затем npm run gen:api
cd frontend && npm ci && npm run gen:api && npm run dev
cd frontend && npm run typecheck && npm run lint && npm test && npm run build

# Production (секреты — в .env.prod, сертификаты — в certs/; оба вне git, шаблон — .env.prod.example)
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.create_admin you@example.com
```

Для превью в Claude Desktop есть `.claude/launch.json`: `api` (uvicorn :8000) и `web` (vite :5173).
