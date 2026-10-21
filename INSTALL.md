# Installation and running

English · [Русский](INSTALL.ru.md)

WirePilot is a control panel for VPN servers. It has four parts:

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
PYTHONPATH=src uv run python -m scripts.create_admin
```

The script asks for the email and offers a password: a **random** one (the default — shown once, stored nowhere, the panel asks you to set your own at the first sign-in) or your own (typed silently, at least 12 characters). `--random-password` skips the questions; `--only-if-empty` does nothing when admins already exist. The default role is `superadmin`; for other roles use `--role operator` or `--role viewer` (it is easier to create them in the panel itself).

Run it from the repository root and **in an interactive terminal**. A random password is shown only in a terminal: if the output goes to a file or a pipe (`| tee`), the script refuses, so the password cannot end up in a log.

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

### Quick install

```bash
git clone <repository-url> tollwire && cd tollwire
./install.sh --domain panel.example.com
```

In one run the installer checks Docker, creates `.env.prod` with random secrets, issues a TLS certificate, starts the panel, waits until it is ready and offers to create the first administrator (a random password is shown once; the panel asks for your own at the first sign-in). The first image build takes a few minutes. Running it again is safe: an existing `.env.prod` and certificate are never touched, the panel is just brought back to a running state. The steps below describe the same thing by hand.

Options: `--domain`, `--cert`/`--key` (your own certificate), `--admin-email`, `--no-start` (only prepare the files). `INSTALL_WAIT_SECONDS` is how long to wait for readiness (300 s by default); the ports `HTTP_PORT`/`HTTPS_PORT` are read from the environment or `.env.prod`. Messages are in English or Russian: `PANEL_LANG=ru|en`, otherwise the system locale.

### If the server cannot download images

Some networks cut connections to Docker's and npm's CDNs after a few kilobytes (the pull stalls with `connection timed out`). Then build the images on a computer with a normal connection and send them over SSH:

```bash
# on that computer, from the repository root (key-based SSH login to the server is required)
./deploy/ship-images.sh my-server        # a name from ~/.ssh/config or user@host
```

It builds `vpn-panel` and `vpn-panel-web` from the committed state (git HEAD), streams them to the server and tags them there. `postgres` and `redis` are pulled by the server by their pinned digests; if that stalls too, ship them the same way (`docker save | gzip | ssh ... 'gunzip | docker load'`) and name them in `.env.prod`: `POSTGRES_IMAGE=...`, `REDIS_IMAGE=...`. On the server start without building:

```bash
./install.sh --domain panel.example.com --no-build       # first install
./panel update --no-pull --no-build                      # later: ship again, then this
```

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

**Automatically** (`./install.sh` does this first): it checks Docker, Compose and openssl and creates `.env.prod` with a random database password, JWT secret and encryption key (mode 600; secrets are never printed). Without `--domain` it asks for the panel address in a terminal. An existing `.env.prod` is never touched; if the file is missing but a database volume from a previous installation (`vpn-panel_pg_data`) is left on the server, the installer stops — new secrets would not fit that database.

**By hand:**

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

**Automatically** (`./install.sh` does this right after `.env.prod`):

- in a terminal it asks which certificate you want: **self-signed** (the default, for a trial: valid 825 days, the browser warns that it does not trust it, the installer prints the SHA-256 fingerprint to compare), **Let's Encrypt** or your own files;
- **Let's Encrypt** (`./install.sh --domain panel.example.com --letsencrypt`, or option 2 in the menu): a real certificate, no browser warning. It needs a real domain name that points at this server and port 80 reachable from the internet. The installer installs `certbot` if missing (apt or dnf, needs sudo), briefly stops the panel's nginx while certbot verifies the domain, and puts the certificate into `certs/`. You accept the [Let's Encrypt terms](https://letsencrypt.org/repository/) yourself: by answering in the menu, or with `--accept-le-terms` when there is no terminal (`--le-email` adds an address for expiry notices). The certificate renews by itself: certbot's system timer runs daily and, when needed, stops nginx for a few seconds and swaps the files. If issuing fails (for example, the ISP blocks incoming connections or the router does not forward port 80) the panel still starts with a self-signed certificate and the installer says how to retry;
- already installed panel: `./panel cert` shows the issuer and expiry, `./panel cert letsencrypt` switches to Let's Encrypt, `./panel cert renew` checks the renewal;
- your own certificate: `./install.sh --cert fullchain.pem --key privkey.pem` — the files are checked (PEM, the key matches the certificate, not expired) and copied into `certs/`. You can also put them into `certs/` yourself and run the installer again;
- existing files in `certs/` are **never replaced**: the installer only checks the pair and warns if the certificate expires soon or does not cover the address. To replace a certificate, delete both files deliberately.

**By hand.** nginx expects two files:

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

`./install.sh` does this for you: it checks that ports 80/443 are free, runs `up -d --build`, and waits until the `api` container is healthy and nginx answers `/health` over HTTPS. By hand:

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
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.create_admin
```

It asks for the email and offers a password: a **random** one (the default — shown once, stored nowhere; the panel asks you to set your own at the first sign-in) or your own (at least 12 characters). `--only-if-empty` does nothing when admins already exist. `./install.sh` runs this step itself when it has a terminal; without one it prints the ready command. Then sign in at `https://<your-domain>`; it is easier to create the other admins in the panel.

### Updating

```bash
./panel update
```

It takes a database backup, runs `git pull --ff-only`, rebuilds and starts the panel (the same `./install.sh` logic, so it waits until the panel is ready). If the folder is not a git repository or tracked files have local changes, it stops before doing anything — update the code yourself and use `--no-pull`; `--no-backup` skips the backup. If the start fails, the message names the backup and the way back (`./panel restore`). By hand:

```bash
git pull
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

New migrations are applied automatically (the `migrate` service). On shutdown the worker waits for the current job (up to 60 s).

### Logs

Logs are JSON, one line per event; a shared `request_id` ties a request in nginx to its handling in the API:

```bash
./panel logs api worker                  # the last 100 lines and follow; --tail N, --no-follow
docker compose --env-file .env.prod -f docker-compose.prod.yml logs --no-log-prefix api | jq .
```

Passwords, keys, tokens and VPN configs are never written to logs. `LOG_LEVEL=debug` in `.env.prod` adds the text of every SSH command (without its output).

### Backup

```bash
./panel backup                           # backups/tollwire-YYYYMMDD-HHMMSS.dump (mode 600), the latest 14 are kept
./panel backup --keep 30 --dir /mnt/backups
./panel restore backups/tollwire-20261016-030000.dump
```

`backup` dumps the database in the custom `pg_dump -Fc` format, checks the file and only then puts it in place; older backups beyond `--keep` are removed (safety copies and foreign files in the folder are never touched). It is safe to run from cron:

```
0 3 * * * cd /opt/tollwire && ./panel backup >> /var/log/tollwire-backup.log 2>&1
```

`restore` **replaces** all panel data with the dump. It asks you to type `restore` (without a terminal use `--yes`), first takes a safety backup of the current data (`tollwire-pre-restore-….dump`; `--no-safety-backup` skips it), stops `api` and `worker`, loads the dump in a single transaction (if it fails, the database stays as it was and the services are started again), applies migrations in case the dump is older than the code, and starts the panel.

**The backup does not contain `.env.prod`, on purpose.** A dump without the `ENCRYPTION_KEY` from your `.env.prod` is useless — and the other way round: someone who steals only the dump cannot read the SSH credentials and VPN configs in it. Keep a copy of `.env.prod` in a password manager or a safe, away from this server and from the backups.

By hand:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backup-$(date +%F).dump
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backup-YYYY-MM-DD.dump
```

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
