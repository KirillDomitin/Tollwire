# CLAUDE.md — VPN Control Panel

Этот файл — инструкция для Claude и карта проекта. Claude читает его в начале каждой сессии.
Подробности по каждой части лежат рядом с кодом в своих `CLAUDE.md` (см. раздел 4) — Claude Code подгружает их
сам, когда работает с файлами этой папки. Здесь — только общее.

---

## 0. РЕЖИМ РАБОТЫ

Код пишет Claude. Пользователь — владелец продукта: принимает архитектурные решения и принимает этапы. (До этапа 11.2 проект шёл в формате наставничества, где код писал пользователь; с 12.1 режим обучения отменён по решению пользователя.)

**Правила:**

1. **Один этап за раз, с остановкой после каждого.** Не забегать вперёд, не делать весь блок разом.
2. **Порядок этапа:**
   1. Коротко: что делаем и зачем. Если есть архитектурная развилка или отступление от ТЗ — вынести на решение пользователя **до** написания кода, с рекомендацией.
   2. Написать код по архитектурным правилам (раздел 3) и требованиям безопасности (раздел 5).
   3. Проверить: `ruff`, `mypy`, миграции (`upgrade`/`downgrade`), и **живой прогон** (HTTP-запросы, при необходимости — реальный сервер). Линтеры логику не ловят — живая проверка обязательна.
   4. Написать теорию этапа в `theory/` (см. ниже).
   5. Обновить `CLAUDE.md` затронутого модуля, если поменялись его устройство, правила или грабли.
   6. Закоммитить и коротко отчитаться: что сделано, какие решения приняты и почему, что проверено, какие пробелы замечены.
   7. Остановиться и обсудить следующий этап. Без явного «погнали» дальше не идти.
3. **Объяснять просто.** Минимум жаргона; англоязычный термин — с переводом в скобках. Пользователь может попросить подробную теорию по этапу — тогда объяснять развёрнуто, с примерами.
4. Действия на реальных серверах, которые что-то меняют (установка/удаление, правка конфигов), — только с явного согласия пользователя.
5. Использовать Context7 MCP для актуальной документации библиотек (FastAPI, SQLAlchemy 2.x, Pydantic v2, Alembic, AsyncSSH и т.д.) — данные обучения могут устареть.

**Прогресс отмечать** в разделе 6 ниже и в [`docs/roadmap.md`](docs/roadmap.md): `[ ]` → `[~]` (в работе) → `[x]` (принято).

**Теория — к каждому этапу, обязательно**: файл `theory/stage-<номер>-<slug>.md` + ссылка в `theory/README.md` (что сделано, зачем, как устроено, грабли, как проверяли; термины с переводом, нейтральные примеры). Пишется до коммита этапа. Папка `theory/` в `.gitignore` — личные заметки пользователя.

---

## 1. О проекте

**VPN Control Panel** — платформа с REST API и веб-интерфейсом для централизованного управления VPN-серверами на базе AmneziaVPN (протокол AmneziaWG): серверы, VPN-пользователи, подписки и доступы, выдача конфигов, установка VPN по SSH, фоновые задачи, мониторинг, аудит.

Полное ТЗ: [`Техническое_задание_—_Backend_платформа_управления_VPN_серверами.md`](Техническое_задание_—_Backend_платформа_управления_VPN_серверами.md) — source of truth по требованиям. Осознанные отступления от ТЗ записаны в `CLAUDE.md` модулей.

Установка и запуск (dev и prod) — [`INSTALL.md`](INSTALL.md).

---

## 2. Технологический стек

- **Бэкенд:** Python 3.14 (`pyproject.toml`), FastAPI, Pydantic v2 + pydantic-settings, PostgreSQL + SQLAlchemy 2.x (async, asyncpg) + Alembic, Redis (очередь, блокировки, лимиты, дедуп планировщика), AsyncSSH. Всё асинхронно.
- **Фронтенд:** React + Vite + TypeScript (strict), TanStack Query, openapi-fetch, uPlot — см. [`frontend/CLAUDE.md`](frontend/CLAUDE.md).
- **Инфраструктура:** Docker, Docker Compose (dev: postgres + redis; prod: api/worker/migrate/postgres/redis/nginx).
- **Качество:** pytest + httpx, Ruff (линт+формат), mypy; фронт — vitest, eslint, tsc.
- **Зависимости:** uv (`uv.lock`), npm (`frontend/package-lock.json`).

---

## 3. Архитектурные правила (нарушать нельзя)

Модульная слоистая архитектура. Направление зависимостей — сверху вниз:

```
API (routers)  →  Service (application)  →  Domain  →  Infrastructure  →  Persistence
```

- **Бизнес-логики в роутерах быть НЕ должно.** Роутер отвечает только за: HTTP, валидацию входа (Pydantic-схемы), авторизацию (`require(Permission.X)`), вызов сервиса, формирование ответа.
- **Работа с VPN — только через абстракцию `VPNProvider`.** Бизнес-логика НЕ знает SSH-команд, Docker-команд, путей к конфигам, структуры контейнеров, способа генерации ключей.

  ```
  Business Logic → VPNProvider (интерфейс) → AmneziaWGProvider → SSHClient → SSH/Docker/Linux
  ```
- **`infrastructure/` не импортирует `modules/`** (кроме воркера и планировщика — они composition root, точка сборки). Поэтому фабрика провайдеров по `ProtocolType` живёт в `modules/servers/service.py`.
- **Работа с БД — через репозитории.** Сервис не пишет сырые запросы вперемешку с логикой.
- **Доменные ошибки** — классы из `core/exceptions.py`; в HTTP-коды их переводят глобальные обработчики в `main.py`, не роутеры.
- Платежей/тарифов пока нет, но `Subscription` + `AccessGrant` — точка расширения под них.

---

## 4. Карта проекта и где читать подробности

```
src/
  main.py                  сборка FastAPI: роутеры, обработчики ошибок, middleware, lifespan
  worker.py                точка входа процесса воркера (задачи + планировщик)
  scripts/                 create_admin, export_openapi
  core/                    → src/core/CLAUDE.md            конфиг, БД, Redis, безопасность, логи, лимиты, ошибки
  infrastructure/
    ssh/                   → .../ssh/CLAUDE.md             SSHClient, ключи хостов
    vpn/                   → .../vpn/CLAUDE.md             VPNProvider, AmneziaWG, awg0.conf, vpn://
    workers/               → .../workers/CLAUDE.md         JobWorker, планировщик, ServerLock
    monitoring/            (описан в modules/monitoring/CLAUDE.md)
  modules/
    auth/                  → CLAUDE.md   админы, вход, JWT, сессии, RBAC
    servers/               → CLAUDE.md   VPNServer, SSH-учётка, ключ хоста, install, sync, health
    protocols/             → CLAUDE.md   VPNProtocol
    vpn_users/             → CLAUDE.md   VPNUser
    access/                → CLAUDE.md   Subscription, AccessGrant, VPNCredential (выдача конфига)
    jobs/                  → CLAUDE.md   Job, очередь на Redis
    audit/                 → CLAUDE.md   AuditLog
    monitoring/            → CLAUDE.md   метрики серверов
alembic/                   → alembic/CLAUDE.md   миграции и грабли Postgres enum
tests/                     → tests/CLAUDE.md     как устроены тесты, фейковый SSH
frontend/                  → frontend/CLAUDE.md  React-интерфейс
docs/roadmap.md            полная дорожная карта
```

Внутри модуля обычно: `models/` (ORM), `schemas.py` (Pydantic), `repository.py`, `service.py`, `api/v1/router.py`.

---

## 5. Требования безопасности (держать в голове всегда)

- Пароли админов — только Argon2id hash.
- Refresh-токены в БД — только в виде hash; в браузер — HttpOnly cookie. Access-токен во фронте — только в памяти.
- SSH- и VPN-секреты — в БД в зашифрованном виде (Fernet); ключ шифрования (`ENCRYPTION_KEY`) — только из ENV, НИКОГДА не в PostgreSQL и не в Git.
- Защита от command injection (никакого пользовательского ввода в SSH-команды — только константы и проверенные по белому списку значения; файлы — через SFTP) и SQL injection (через SQLAlchemy).
- В логи НЕ попадают: пароли, приватные ключи (SSH/VPN), refresh-токены, JWT, полные VPN-конфиги, stdout SSH-команд.
- SSH: connect timeout и command timeout обязательны; пароль SSH отправляется только серверу с закреплённым ключом хоста.
- Все критические операции идемпотентны (повторный запуск не ломает состояние).
- Каждый эндпоинт требует ровно одно право (`require(Permission.X)`), матрица — в `modules/auth/CLAUDE.md`.

---

## 6. Дорожная карта — сводка

Полный список этапов с описаниями — [`docs/roadmap.md`](docs/roadmap.md).

- **Сделано:** блоки 0–2, 4–19 (бэкенд, MVP + доводка ТЗ + исправления по ревью), 20.1–20.3 (фронтенд: вход, серверы, пользователи/доступ/конфиг), 21.1 (конфиг `vpn://`), 21.2 (сбор метрик), 21.3 (мониторинг в интерфейсе).
- **Открыто:**
  - [ ] 3.1 Эталон слоёв (фактически покрыт модулем auth — отдельно не делали)
  - [ ] 20.4 Журнал аудита, задачи, админы во фронтенде (заготовка `frontend/src/lib/audit.ts` не закоммичена)
  - [ ] 21.4 (опц.) Онлайн и трафик по клиентам
- **Кандидаты (не утверждены):** сверка удаляет только клиентов своей панели (метка панели в имени peer'а — сейчас две панели на одном сервере удаляют клиентов друг друга); health различает «недоступен» и «ключ хоста не закреплён»; плавающий тест `test_login_limited_by_ip`; `--restart` у контейнера amnezia-awg; ротация ключа шифрования (MultiFernet).

---

## 7. Полезные команды

```bash
uv sync                                                 # установить зависимости
uv run uvicorn main:app --app-dir src --reload          # API (dev); без --app-dir: ModuleNotFoundError: core
uv run python src/worker.py                             # воркер (задачи + планировщик)
uv run ruff check . && uv run ruff format .             # линт и формат
uv run mypy src                                         # типы
uv run pytest                                           # тесты (нужен docker compose up: БД vpn_panel_test, Redis db 15)
uv run pytest --cov --cov-report=term-missing           # с покрытием
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
