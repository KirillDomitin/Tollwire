# Установка и запуск

VPN Control Panel — это четыре части:

| Часть | Что делает | Как запускается |
|---|---|---|
| **API** | REST API на FastAPI | `uvicorn` |
| **Воркер** | фоновые задачи (установка, выдача конфигов, проверки) и планировщик | `python src/worker.py` |
| **Фронтенд** | админ-панель (React + Vite) | dev-сервер Vite / статика в nginx |
| **PostgreSQL + Redis** | данные; очередь задач и блокировки | Docker |

Без воркера панель открывается, но задачи (установка, выдача и отзыв конфигов, проверки) стоят в очереди и не выполняются.

- [Разработка (dev)](#разработка-dev)
- [Продакшен (prod)](#продакшен-prod)
- [Частые проблемы](#частые-проблемы)

---

## Разработка (dev)

Локально: PostgreSQL и Redis — в Docker, API, воркер и фронтенд — прямо на машине.

### Что нужно установить

| Инструмент | Версия | Зачем |
|---|---|---|
| Docker + Docker Compose | любая свежая | PostgreSQL и Redis |
| Python | 3.12+ (в образе — 3.14) | бэкенд |
| [uv](https://docs.astral.sh/uv/) | свежая | зависимости Python (строго по `uv.lock`) |
| Node.js | 24 | фронтенд |

Команды ниже — для bash (Linux, macOS, Git Bash на Windows). Отличия для PowerShell — в конце раздела.

### 1. Зависимости

```bash
git clone <адрес репозитория> AmneziaAdminPanel
cd AmneziaAdminPanel
uv sync
cd frontend && npm ci && cd ..
```

### 2. PostgreSQL и Redis

```bash
docker compose up -d
```

Поднимает `postgres` (пользователь `user`, пароль `password`, база `vpn_panel`) и `redis`. Оба слушают **только localhost** (`127.0.0.1:5432`, `127.0.0.1:6379`) — пароли тут учебные, наружу их не открываем.

### 3. Файл `.env`

```bash
cp .env.example .env
```

В `.env` обязательно заменить два значения (с заглушками приложение не запустится или будет небезопасно):

```bash
# JWT_SECRET_KEY — не короче 32 символов и не «change_me…» (иначе API не стартует)
uv run python -c "import secrets; print(secrets.token_hex(32))"

# ENCRYPTION_KEY — ключ Fernet, им шифруются SSH-учётки и VPN-конфиги в БД
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Остальное в `.env.example` уже настроено под dev (`DATABASE_URL`, `REDIS_URL`, `COOKIE_SECURE=false` — локально API работает по http).

> `ENCRYPTION_KEY` нельзя менять после того, как в базу записали SSH-учётки или конфиги: старые записи станут нечитаемыми.

### 4. Миграции

```bash
uv run alembic upgrade head
```

### 5. Первый админ

```bash
PYTHONPATH=src uv run python -m scripts.create_admin you@example.com
```

Пароль спросит скрыто (не короче 12 символов). Роль по умолчанию — `superadmin`; для других ролей — `--role operator` или `--role viewer` (их удобнее заводить в самой панели).

Запускать из корня репозитория и **в интерактивном терминале**: пароль читается с консоли, через pipe его не передать.

### 6. Запуск

Три процесса, каждый в своём терминале:

```bash
# API → http://localhost:8000
uv run uvicorn main:app --app-dir src --reload --port 8000
```

```bash
# Воркер (задачи + планировщик)
uv run python src/worker.py
```

```bash
# Фронтенд → http://localhost:5173
cd frontend && npm run dev
```

Панель — **http://localhost:5173**. Vite проксирует `/api` на `localhost:8000`, поэтому для браузера фронтенд и API — один адрес (как в проде): refresh-cookie работает, CORS не нужен.

Документация API (Swagger) — http://localhost:8000/api/openapi, проверка живости — http://localhost:8000/health.

> **Воркер подключается к настоящим серверам.** Его планировщик периодически запускает проверку состояния (health) и сверку (sync) всех серверов из базы. Если в dev-базе есть боевые серверы — помните об этом, или выключите планировщик: `SCHEDULER_ENABLED=false` в `.env`.

### Проверки перед коммитом

```bash
# бэкенд
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest          # нужен docker compose up: тесты создают свою БД vpn_panel_test и берут Redis db 15

# фронтенд
cd frontend
npm run typecheck && npm run lint && npm test && npm run build
```

### Изменили API — обновите типы фронтенда

Фронтенд берёт типы запросов и ответов из OpenAPI-схемы бэкенда. После изменения схем или эндпоинтов:

```bash
PYTHONPATH=src uv run python -m scripts.export_openapi frontend/openapi.json
cd frontend && npm run gen:api
```

Сервер для этого запускать не нужно — схема выгружается из кода.

### PowerShell (Windows)

Отличается только установка переменной окружения:

```powershell
$env:PYTHONPATH = "src"; uv run python -m scripts.create_admin you@example.com
$env:PYTHONPATH = "src"; uv run python -m scripts.export_openapi frontend/openapi.json
```

---

## Продакшен (prod)

Всё в Docker (`docker-compose.prod.yml`):

```
интернет ── 80/443 ──► nginx ──► api ──► postgres, redis
                    (статика      worker ──► postgres, redis
                     фронтенда)           └─► SSH к VPN-серверам
```

- наружу открыт **только nginx** (80 → редирект на 443, HTTPS);
- `api`, `worker` и одноразовый `migrate` — один образ из корневого `Dockerfile`;
- `postgres` и `redis` — во внутренней сети без выхода в интернет;
- миграции применяются автоматически при каждом запуске (`migrate`), api и воркер стартуют только после них;
- документация API (`/api/openapi`) в проде выключена.

### Что нужно

- Linux-сервер с Docker и Docker Compose (плагин `docker compose`);
- домен, указывающий на сервер, и TLS-сертификат для него;
- открытые входящие порты 80 и 443; исходящий доступ по SSH к VPN-серверам (его использует воркер).

### 1. Код

```bash
git clone <адрес репозитория> vpn-panel
cd vpn-panel
```

### 2. Секреты: `.env.prod`

```bash
cp .env.prod.example .env.prod
chmod 600 .env.prod
```

Заполнить:

| Переменная | Чем заполнить |
|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -hex 32` — только `[0-9a-f]`, пароль подставляется в URL базы |
| `JWT_SECRET_KEY` | `openssl rand -hex 32` |
| `ENCRYPTION_KEY` | ключ Fernet — см. ниже |

Ключ Fernet, если на сервере нет Python с `cryptography`, — через собранный образ приложения:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml build api
docker run --rm vpn-panel:latest python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`DATABASE_URL`, `REDIS_URL`, `COOKIE_SECURE`, `LOG_FORMAT` в `.env.prod` не задаются — их собирает `docker-compose.prod.yml`.

> **`ENCRYPTION_KEY` — самое ценное.** Им зашифрованы SSH-учётки серверов и VPN-конфиги в базе. Потеряете — бэкап базы бесполезен; смените — старые записи не расшифровать. Храните копию отдельно от сервера (менеджер паролей, сейф). `.env.prod` и `certs/` — в `.gitignore`, в git они не попадают.

### 3. TLS-сертификат

nginx ждёт два файла:

```
certs/fullchain.pem   — сертификат с цепочкой
certs/privkey.pem     — приватный ключ
```

Например, из Let's Encrypt: получить сертификат (certbot) и скопировать `fullchain.pem` и `privkey.pem` в `certs/`. После обновления сертификата — `docker compose --env-file .env.prod -f docker-compose.prod.yml restart nginx`.

Для пробного запуска без домена подойдёт самоподписанный сертификат (браузер предупредит):

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=localhost" \
  -keyout certs/privkey.pem -out certs/fullchain.pem
```

### 4. Запуск

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

`--env-file .env.prod` обязателен: из него берутся пароль базы и порты для самого compose-файла.

Проверка:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml ps   # migrate — exited (0), остальные — running/healthy
curl -fsS https://<ваш-домен>/health
```

### 5. Первый админ

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.create_admin you@example.com
```

Пароль спросит в терминале. Дальше — вход на `https://<ваш-домен>`, остальных админов удобнее заводить в панели.

### Обновление

```bash
git pull
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

Новые миграции применятся сами (сервис `migrate`). Воркер при остановке дожидается текущей задачи (до 60 с).

### Логи

Логи — в JSON, по строке на событие; запрос в nginx и его обработку в api связывает общий `request_id`:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f api worker
docker compose --env-file .env.prod -f docker-compose.prod.yml logs --no-log-prefix api | jq .
```

Пароли, ключи, токены и VPN-конфиги в логи не пишутся. `LOG_LEVEL=debug` в `.env.prod` добавляет текст каждой SSH-команды (без её вывода).

### Резервная копия

```bash
# бэкап базы
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backup-$(date +%F).dump

# восстановление (в пустую базу)
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backup-YYYY-MM-DD.dump
```

Бэкап без `ENCRYPTION_KEY` из того же `.env.prod` бесполезен — храните их вместе (но не на том же сервере).

### Остановка

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml down
```

Данные (тома `pg_data`, `redis_data`) остаются. **`down -v` удаляет тома — то есть всю базу.**

### Настройки, которые можно менять в `.env.prod`

| Переменная | По умолчанию | Что |
|---|---|---|
| `HTTP_PORT`, `HTTPS_PORT` | 80, 443 | порты nginx на хосте |
| `EDGE_SUBNET` | 172.30.240.0/24 | подсеть внешней docker-сети — сменить, если пересекается с сетями хоста |
| `LOG_LEVEL` | info | `debug` — подробнее |
| `SCHEDULER_ENABLED` и `*_INTERVAL_SECONDS` | см. шаблон | планировщик: проверки, сверка, истечение доступов, чистка (0 — правило выключено) |
| `JOB_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS` | 90, 365 | сколько хранить задачи и журнал (0 — вечно) |
| `LOGIN_*`, `REFRESH_*` | см. шаблон | лимиты попыток входа |

---

## Частые проблемы

| Симптом | Причина и решение |
|---|---|
| API не стартует: `JWT_SECRET_KEY must be at least 32 random characters` | в `.env` осталась заглушка из шаблона — сгенерируйте ключ (шаг 3) |
| `ModuleNotFoundError: No module named 'core'` | API запущен без `--app-dir src`, или скрипт — без `PYTHONPATH=src` |
| скрипт не видит `.env` (ошибки про `DATABASE_URL`) | скрипты запускаются **из корня** репозитория — `.env` ищется в текущей папке |
| задачи висят «В очереди» | не запущен воркер |
| вход проходит, но после перезагрузки страницы снова форма входа (dev) | в `.env` нет `COOKIE_SECURE=false`: по http браузер не сохраняет cookie с флагом Secure |
| dev-фронтенд: ошибки сети на `/api` | не запущен API на `:8000` — Vite проксирует туда |
| prod: `migrate` упал, api/worker не стартуют | `docker compose ... logs migrate` — обычно неверный `POSTGRES_*` или нестандартные символы в пароле |
| prod: nginx не стартует | нет `certs/fullchain.pem` / `certs/privkey.pem` |
| prod: «порт занят» | на хосте уже что-то слушает 80/443 — освободите или задайте `HTTP_PORT`/`HTTPS_PORT` (при нестандартном HTTPS-порте редирект с http уводит на 443 без порта — открывайте сразу `https://…:<порт>`) |
| задача сервера упала с `Host key of … is not pinned` | на карточке сервера закрепите ключ хоста (первый шаг подготовки) — пароль уходит только на сервер с закреплённым ключом |
