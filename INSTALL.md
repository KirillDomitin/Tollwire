# Installation and running

English · [Русский](INSTALL.ru.md)

Tollwire is a control panel for VPN servers. It has four parts:

| Part | What it does | How it runs |
|---|---|---|
| **API** | REST API on FastAPI | `uvicorn` |
| **Worker** | background jobs (install, issue configs, checks) and the scheduler | `python src/worker.py` |
| **Frontend** | admin panel (React + Vite) | Vite dev server / static files in nginx |
| **PostgreSQL + Redis** | data; job queue and locks | Docker |

Without the worker the panel opens, but jobs (install, issue and revoke configs, checks) stay in the queue and never run.

- [Development](#development)
- [Production](#production)
- [Troubleshooting](#troubleshooting)

---

## Development

Locally: PostgreSQL and Redis run in Docker; the API, the worker and the frontend run directly on your machine.

### What to install

| Tool | Version | Used for |
|---|---|---|
| Docker + Docker Compose | any recent | PostgreSQL and Redis |
| Python | 3.12+ (3.14 in the image) | backend |
| [uv](https://docs.astral.sh/uv/) | recent | Python dependencies (strictly from `uv.lock`) |
| Node.js | 24 | frontend |

The commands below are for bash (Linux, macOS, Git Bash on Windows). PowerShell differences are at the end of this section.

### 1. Dependencies

```bash
git clone <repository-url> tollwire
cd tollwire
uv sync
cd frontend && npm ci && cd ..
```

### 2. PostgreSQL and Redis

```bash
docker compose up -d
```

This starts `postgres` (user `user`, password `password`, database `vpn_panel`) and `redis`. Both listen on **localhost only** (`127.0.0.1:5432`, `127.0.0.1:6379`) — the passwords are for development and must never be exposed.

### 3. The `.env` file

```bash
cp .env.example .env
```

Two values in `.env` must be replaced (with the placeholders the app will not start, or will be insecure):

```bash
# JWT_SECRET_KEY — at least 32 characters and not "change_me…" (otherwise the API will not start)
uv run python -c "import secrets; print(secrets.token_hex(32))"

# ENCRYPTION_KEY — a Fernet key; it encrypts SSH credentials and VPN configs in the database
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Everything else in `.env.example` is already set for development (`DATABASE_URL`, `REDIS_URL`, `COOKIE_SECURE=false` — locally the API runs over http).

> Do not change `ENCRYPTION_KEY` once SSH credentials or configs have been written to the database: the old records become unreadable.

### 4. Migrations

```bash
uv run alembic upgrade head
```

### 5. First admin

```bash
PYTHONPATH=src uv run python -m scripts.create_admin you@example.com
```

The password is asked for silently (at least 12 characters). The default role is `superadmin`; for other roles use `--role operator` or `--role viewer` (it is easier to create them in the panel itself).

Run it from the repository root and **in an interactive terminal**: the password is read from the console and cannot be piped in.

### 6. Run

Three processes, each in its own terminal:

```bash
# API → http://localhost:8000
uv run uvicorn main:app --app-dir src --reload --port 8000
```

```bash
# Worker (jobs + scheduler)
uv run python src/worker.py
```

```bash
# Frontend → http://localhost:5173
cd frontend && npm run dev
```

The panel is at **http://localhost:5173**. Vite proxies `/api` to `localhost:8000`, so for the browser the frontend and the API share one origin (as in production): the refresh cookie works and CORS is not needed.

API documentation (Swagger) is at http://localhost:8000/api/openapi, the liveness check at http://localhost:8000/health.

> **The worker connects to real servers.** Its scheduler periodically runs health checks and sync for every server in the database. If your dev database contains production servers, keep that in mind, or turn the scheduler off with `SCHEDULER_ENABLED=false` in `.env`.

### Checks before a commit

```bash
# backend
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest          # needs docker compose up: tests create their own database vpn_panel_test and use Redis db 15

# frontend
cd frontend
npm run typecheck && npm run lint && npm test && npm run build
```

### Changed the API? Update the frontend types

The frontend takes request and response types from the backend's OpenAPI schema. After changing schemas or endpoints:

```bash
PYTHONPATH=src uv run python -m scripts.export_openapi frontend/openapi.json
cd frontend && npm run gen:api
```

No server needs to run for this — the schema is exported from the code.

### PowerShell (Windows)

Only setting the environment variable differs:

```powershell
$env:PYTHONPATH = "src"; uv run python -m scripts.create_admin you@example.com
$env:PYTHONPATH = "src"; uv run python -m scripts.export_openapi frontend/openapi.json
```

---

## Production

Everything runs in Docker (`docker-compose.prod.yml`):

```
internet ── 80/443 ──► nginx ──► api ──► postgres, redis
                    (frontend     worker ──► postgres, redis
                     static files)         └─► SSH to VPN servers
```

- **only nginx** is exposed (80 redirects to 443, HTTPS);
- `api`, `worker` and the one-shot `migrate` share one image built from the root `Dockerfile`;
- `postgres` and `redis` sit on an internal network with no internet access;
- migrations are applied on every start (`migrate`); the API and the worker start only after it finishes;
- the API documentation (`/api/openapi`) is disabled in production.

### Requirements

- a Linux server with Docker and Docker Compose (the `docker compose` plugin);
- a domain pointing at the server and a TLS certificate for it;
- inbound ports 80 and 443 open; outbound SSH access to the VPN servers (the worker uses it).

### 1. Code

```bash
git clone <repository-url> tollwire
cd tollwire
```

### 2. Secrets: `.env.prod`

**Автоматически:**

```bash
./install.sh --domain panel.example.com
```

Установщик проверяет Docker, Compose и openssl и создаёт `.env.prod` со случайными паролем БД, JWT-секретом и ключом шифрования (права 600, на экран секреты не выводятся). Без `--domain` в терминале спросит адрес панели. Повторный запуск **не трогает** существующий `.env.prod`; если файла нет, но на сервере остался том базы от прошлой установки (`vpn-panel_pg_data`), установщик остановится: новые секреты к этой базе не подойдут. Сертификат, запуск и админа пока делаются вручную (шаги 3–5); их он возьмёт на себя в следующих версиях.

**Вручную:**

```bash
cp .env.prod.example .env.prod
chmod 600 .env.prod
```

Fill in:

| Variable | What to put |
|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -hex 32` — only `[0-9a-f]`, the password is substituted into the database URL |
| `JWT_SECRET_KEY` | `openssl rand -hex 32` |
| `ENCRYPTION_KEY` | a Fernet key — see below |

If the server has no Python with `cryptography`, generate the Fernet key with the built application image:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml build api
docker run --rm vpn-panel:latest python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`DATABASE_URL`, `REDIS_URL`, `COOKIE_SECURE` and `LOG_FORMAT` are not set in `.env.prod` — `docker-compose.prod.yml` builds them.

> **`ENCRYPTION_KEY` is the most valuable thing here.** It encrypts the servers' SSH credentials and the VPN configs in the database. Lose it and a database backup is useless; change it and the old records cannot be decrypted. Keep a copy away from the server (a password manager, a safe). `.env.prod` and `certs/` are in `.gitignore` and never reach git.

### 3. TLS certificate

nginx expects two files:

```
certs/fullchain.pem   — certificate with the chain
certs/privkey.pem     — private key
```

For example, from Let's Encrypt: obtain a certificate (certbot) and copy `fullchain.pem` and `privkey.pem` into `certs/`. After renewing the certificate run `docker compose --env-file .env.prod -f docker-compose.prod.yml restart nginx`.

For a trial run without a domain a self-signed certificate will do (the browser will warn):

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=localhost" \
  -keyout certs/privkey.pem -out certs/fullchain.pem
```

### 4. Start

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

`--env-file .env.prod` is required: compose itself takes the database password and ports from it.

Check:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml ps   # migrate — exited (0), the rest — running/healthy
curl -fsS https://<your-domain>/health
```

### 5. First admin

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.create_admin you@example.com
```

The password is asked for in the terminal. Then sign in at `https://<your-domain>`; it is easier to create the other admins in the panel.

### Updating

```bash
git pull
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

New migrations are applied automatically (the `migrate` service). On shutdown the worker waits for the current job (up to 60 s).

### Logs

Logs are JSON, one line per event; a shared `request_id` ties a request in nginx to its handling in the API:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f api worker
docker compose --env-file .env.prod -f docker-compose.prod.yml logs --no-log-prefix api | jq .
```

Passwords, keys, tokens and VPN configs are never written to logs. `LOG_LEVEL=debug` in `.env.prod` adds the text of every SSH command (without its output).

### Backup

```bash
# back up the database
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backup-$(date +%F).dump

# restore (into an empty database)
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backup-YYYY-MM-DD.dump
```

A backup without the `ENCRYPTION_KEY` from the same `.env.prod` is useless — keep them together (but not on the same server).

### Stop

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml down
```

The data (volumes `pg_data`, `redis_data`) stays. **`down -v` deletes the volumes — that is, the whole database.**

### Settings you can change in `.env.prod`

| Variable | Default | What it does |
|---|---|---|
| `HTTP_PORT`, `HTTPS_PORT` | 80, 443 | nginx ports on the host |
| `EDGE_SUBNET` | 172.30.240.0/24 | subnet of the external docker network — change it if it overlaps the host's networks |
| `LOG_LEVEL` | info | `debug` — more detail |
| `SCHEDULER_ENABLED` and `*_INTERVAL_SECONDS` | see the template | scheduler: checks, sync, access expiry, cleanup (0 turns a rule off) |
| `JOB_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS` | 90, 365 | how long to keep jobs and the audit log (0 — forever) |
| `LOGIN_*`, `REFRESH_*` | see the template | login attempt limits |

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The API does not start: `JWT_SECRET_KEY must be at least 32 random characters` | the placeholder from the template is still in `.env` — generate a key (step 3) |
| `ModuleNotFoundError: No module named 'core'` | the API was started without `--app-dir src`, or a script without `PYTHONPATH=src` |
| a script does not see `.env` (errors about `DATABASE_URL`) | scripts must run **from the repository root** — `.env` is looked up in the current folder |
| jobs hang in "Queued" | the worker is not running |
| sign-in works, but after a page reload the login form is back (dev) | `.env` lacks `COOKIE_SECURE=false`: over http the browser does not keep a cookie with the Secure flag |
| dev frontend: network errors on `/api` | the API is not running on `:8000` — Vite proxies there |
| prod: `migrate` failed, api/worker do not start | `docker compose ... logs migrate` — usually a wrong `POSTGRES_*` or unusual characters in the password |
| prod: nginx does not start | `certs/fullchain.pem` / `certs/privkey.pem` are missing |
| prod: "port in use" | something on the host already listens on 80/443 — free it or set `HTTP_PORT`/`HTTPS_PORT` (with a non-standard HTTPS port the http redirect sends you to 443 without the port — open `https://…:<port>` directly) |
| a server job failed with `Host key of … is not pinned` | pin the host key on the server's page (the first preparation step) — the password is sent only to a server with a pinned key |
