# CLAUDE.md — VPN Control Panel

Этот файл — инструкция для Claude и карта проекта. Claude читает его в начале каждой сессии.

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
   5. Закоммитить и коротко отчитаться: что сделано, какие решения приняты и почему, что проверено, какие пробелы замечены.
   6. Остановиться и обсудить следующий этап. Без явного «погнали» дальше не идти.
3. **Объяснять просто.** Минимум жаргона; англоязычный термин — с переводом в скобках. Пользователь может попросить подробную теорию по этапу — тогда объяснять развёрнуто, с примерами.
4. Действия на реальных серверах, которые что-то меняют (установка/удаление, правка конфигов), — только с явного согласия пользователя.
5. Использовать Context7 MCP для актуальной документации библиотек (FastAPI, SQLAlchemy 2.x, Pydantic v2, Alembic, AsyncSSH и т.д.) — данные обучения могут устареть.

**Прогресс отмечать** в разделе «Дорожная карта» ниже: менять статус этапа `[ ]` → `[~]` (в работе) → `[x]` (принято).

**Теория — к каждому этапу, обязательно**: файл `theory/stage-<номер>-<slug>.md` + ссылка в `theory/README.md` (что сделано, зачем, как устроено, грабли, как проверяли; термины с переводом, нейтральные примеры). Пишется до коммита этапа. Папка `theory/` в `.gitignore` — личные заметки пользователя.

---

## 1. О проекте

**VPN Control Panel** — backend-платформа с REST API для централизованного управления VPN-серверами на базе AmneziaVPN: серверы, VPN-пользователи, выдача доступов, установка VPN по SSH, фоновые задачи, аудит.

Полное ТЗ: [`Техническое_задание_—_Backend_платформа_управления_VPN_серверами.md`](Техническое_задание_—_Backend_платформа_управления_VPN_серверами.md) — это source of truth по требованиям.

---

## 2. Технологический стек

- Python 3.10+ (в проекте пока стоит 3.14 в `pyproject.toml`)
- FastAPI, Pydantic v2, pydantic-settings
- PostgreSQL + SQLAlchemy 2.x (async, asyncpg) + Alembic
- Redis (очередь задач, distributed locks, cache, rate limiting)
- AsyncSSH (подключение к Linux-серверам)
- asyncio везде: и БД, и SSH строго асинхронно
- Docker, Docker Compose
- pytest, httpx — тесты
- Ruff (линт+формат), mypy (типы)
- Менеджер зависимостей: **uv** (в репо есть `uv.lock`)

---

## 3. Архитектурные правила (нарушать нельзя)

Модульная слоистая архитектура. Направление зависимостей — сверху вниз:

```
API (routers)  →  Service (application)  →  Domain  →  Infrastructure  →  Persistence
```

- **Бизнес-логики в роутерах быть НЕ должно.** Роутер отвечает только за: HTTP, валидацию входа (Pydantic-схемы), авторизацию, вызов сервиса, формирование ответа.
- **Работа с VPN — только через абстракцию `VPNProvider`.** Бизнес-логика НЕ знает SSH-команд, Docker-команд, путей к конфигам, структуры контейнеров, способа генерации ключей. Всё это спрятано за провайдером.

  ```
  Business Logic → VPNProvider (интерфейс) → AmneziaProvider → Infrastructure → SSH/Docker/Linux
  ```
- **Работа с БД — через репозитории.** Сервис не пишет сырые запросы вперемешку с логикой.
- Модели платежей/подписок/тарифов пока не делаем, но архитектура должна позволять их добавить без переписывания доступов (`AccessGrant` — точка расширения).

---

## 4. Структура проекта (целевая)

```
src/
  main.py                     # сборка FastAPI-приложения
  core/                       # общие вещи, не привязанные к домену
    config.py                 # настройки из ENV (pydantic-settings)
    database/postgres.py      # AsyncEngine, async_sessionmaker, get_session
    redis.py                  # подключение к Redis
    security.py               # хеш паролей, JWT, шифрование секретов
    exceptions.py             # базовые доменные исключения
  infrastructure/
    ssh/                      # SSHClient (AsyncSSH-обёртка)
    vpn/                      # VPNProvider, AmneziaProvider, installer
    workers/                  # фоновый воркер
  modules/
    auth/                     # админы, вход, JWT, сессии
    servers/                  # VPNServer, credentials, sync, health
    protocols/                # VPNProtocol
    vpn_users/                # VPNUser
    access/                   # AccessGrant, VPNCredential
    jobs/                     # Job, очередь
    audit/                    # AuditLog
```

Каждый модуль внутри обычно: `models/` (ORM), `schemas.py` (Pydantic), `repository.py`, `service.py`, `api/v1/` (роутеры).

---

## 5. Требования безопасности (держать в голове всегда)

- Пароли админов — только Argon2id hash.
- Refresh-токены в БД — только в виде hash, не в открытом виде; в браузер — HttpOnly cookie.
- SSH- и VPN-секреты — в БД в зашифрованном виде; ключ шифрования (`ENCRYPTION_KEY`) — только из ENV/Docker Secret, НИКОГДА не в PostgreSQL и не в Git.
- Защита от command injection (не конкатенировать пользовательский ввод в SSH-команды) и SQL injection (через SQLAlchemy).
- В логи НЕ попадают: пароли, приватные ключи (SSH/VPN), refresh-токены, JWT, полные VPN-конфиги.
- SSH: connect timeout и command timeout обязательны.
- Все критические операции идемпотентны (повторный запуск не ломает состояние).

---

## 6. Дорожная карта (этапы)

Легенда: `[ ]` не начато · `[~]` в работе · `[x]` принято.

### Блок 0. Фундамент
- [x] 0.1 Окружение: зависимости (uv), `pyproject`, структура, Ruff+mypy, `.env`/`.env.example`
- [x] 0.2 Docker Compose для dev: postgres + redis
- [x] 0.3 Конфиг через pydantic-settings (`core/config.py`)
- [x] 0.4 Async-подключение к PostgreSQL (`AsyncEngine`, `async_sessionmaker`, `get_session`) + healthcheck endpoint

### Блок 1. База данных и миграции
- [x] 1.1 Declarative Base (SQLAlchemy 2.x), общий базовый класс модели
- [x] 1.2 Настройка Alembic под async
- [x] 1.3 Модель `Admin` + первая миграция

### Блок 2. Аутентификация
- [x] 2.1 Хеширование паролей Argon2id (`core/security.py`)
- [x] 2.2 JWT access token (выпуск/проверка)
- [x] 2.3 Refresh token + `admin_sessions` (хранение hash) + HttpOnly cookie
- [x] 2.4 Endpoints: `login`, `refresh`, `logout`, `me`
- [x] 2.5 Dependency «текущий админ» (авторизация роутеров)

### Блок 3. Эталон слоёв
- [ ] 3.1 Паттерн repository → service → schema → router на одном простом примере

### Блок 4. Серверы
- [x] 4.1 Модель `VPNServer` + статусы (NEW/INSTALLING/READY/OFFLINE/ERROR)
- [x] 4.2 CRUD `/api/v1/servers`
- [x] 4.3 `server_credentials` + шифрование секретов (`ENCRYPTION_KEY`)

### Блок 5. SSH-подсистема
- [x] 5.1 `SSHClient` (AsyncSSH): connect/execute/upload/download/close
- [x] 5.2 Таймауты, retry, обработка ошибок аутентификации/соединения, безопасное закрытие
- [x] 5.3 `test-connection` endpoint

### Блок 6. VPN Provider
- [x] 6.1 Интерфейс `VPNProvider` (install/uninstall/get_status/create_client/revoke_client/list_clients/sync)
- [x] 6.2 Скелет `AmneziaProvider` (`AmneziaInstaller`, `AmneziaWGProvider`)

### Блок 7. Jobs и Worker
- [x] 7.1 Модель `Job` + статусы/типы
- [x] 7.2 Очередь задач на Redis
- [x] 7.3 Отдельный процесс `vpn-admin-worker` (asyncio)
- [x] 7.4 Distributed locks на Redis (TTL + безопасное освобождение)
- [x] 7.5 Endpoints `/api/v1/jobs`, `/api/v1/jobs/{id}`

### Блок 8. Установка Amnezia (сквозной сценарий)
- [x] 8.1 `install`/`uninstall` через Job → worker → SSH → verification → sync

### Блок 9. Протоколы
- [x] 9.1 Модель `VPNProtocol` (AMNEZIA_WG), установка/удаление протокола

### Блок 10. VPN-пользователи
- [x] 10.1 Модель `VPNUser` (не связан с `Admin`) + CRUD `/api/v1/vpn-users`

### Блок 11. Доступы (AccessGrant)
- [x] 11.1 Модель `AccessGrant` + статусы (PENDING/ACTIVE/EXPIRED/REVOKED)
- [x] 11.2 Выдача/изменение/отзыв доступа + проверки конфликтов
- [x] 11.3 Ограничение по времени (`valid_from`/`valid_until`) + идемпотентный revoke

### Блок 12. VPN-конфигурации
- [x] 12.1 Модель `VPNCredential` (шифрованный config) + защищённая выдача конфигурации
- [x] 12.2 Реальный `create_client`/`revoke_client` в `AmneziaWGProvider` (аллокатор адресов, `[Peer]` на сервере, сборка конфига клиента)
- [x] 12.3 Выдача/отзыв клиента через Job (`CREATE_USER`/`REVOKE_ACCESS`), эндпоинты, отзыв на сервере при отзыве гранта/подписки

### Блок 13. Синхронизация и Health
- [x] 13.1 `sync` сервера (сверка реального состояния с БД)
- [x] 13.2 `health` (SSH/Docker/Amnezia/protocol/service) + сохранение результата

### Блок 14. Аудит
- [x] 14.1 Модель `AuditLog` + запись критических действий (без секретов)

### Блок 15. Фоновые правила
- [x] 15.1 Периодический worker: перевод просроченных `AccessGrant` в EXPIRED + revoke на сервере

### Блок 16. Тесты
- [x] 16.1 Unit: сервисы, репозитории, провайдеры, переходы статусов, expiration
- [x] 16.2 Integration: PostgreSQL, Redis, API, auth (fake/mock SSH-слой)

### Блок 17. Production
- [x] 17.1 Docker: единый image для api+worker; compose: api/worker/postgres/redis/nginx
- [x] 17.2 Структурированные логи (request_id, job_id, server_id)

### Блок 18. Доводка требований ТЗ (после MVP)
- [x] 18.1 Rate limiting (Redis + грубый лимит nginx) + защита login от тайминг-атаки
- [x] 18.2 RBAC (роли superadmin/operator/viewer, права на каждый эндпоинт, /api/v1/admins)
- [x] 18.3 Проверка ключа SSH-сервера (known_hosts / TOFU)

### Блок 19. Исправления по ревью (fastapi-reviewer + security-reviewer)
- [x] 19.1 Корректность и безопасность: пароль SSH только на закреплённый ключ, смена host удаляет учётку, вход по SSH-ключу, только свои ключи (без ~/.ssh и агента), удалён PATCH /access, гонка выдачи/отзыва, TTL блокировки сервера, Argon2 вне event loop
- [ ] 19.2 Надёжность: ошибки 500, зависшие задачи, rollback в воркере, сбои Redis, атомарный refresh, дубли подписок, Job.error, валидация, логика из роутеров в сервисы
- [ ] 19.3 Hardening: секреты, compose, openapi, образы, атомарная запись конфигов, allowlist клиентского конфига

### Definition of Done (MVP)
Админ может: войти → добавить сервер → проверить SSH → установить Amnezia → установить AmneziaWG → смотреть job/статус сервера → создать VPN-пользователя → выдать доступ → получить конфиг → изменить срок → отозвать доступ → удалить пользователя → sync → health → смотреть audit log.

---

## 7. Полезные команды

```bash
uv sync                 # установить зависимости
uv run uvicorn src.main:app --reload   # запуск API (dev)
uv run ruff check .     # линт
uv run ruff format .    # форматирование
uv run mypy src         # проверка типов
uv run pytest           # тесты (tests/services нужен docker compose up: своя БД vpn_panel_test, Redis db 15)
uv run pytest --cov --cov-report=term-missing   # с отчётом покрытия
uv run alembic upgrade head            # применить миграции
cd src && uv run python -m scripts.create_admin you@example.com   # создать админа (пароль спросит скрыто)

# Production (секреты — в .env.prod, сертификаты — в certs/; оба вне git, шаблон — .env.prod.example)
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.create_admin you@example.com
```

(Точный запуск уточняется на этапе 0 — здесь ориентир.)
