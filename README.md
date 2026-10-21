<div align="center">

# WirePilot

**A self-hosted control panel for AmneziaWG VPN servers.**
Add servers, install the VPN over SSH, issue access to people with an expiry date, watch server health, and keep an audit trail of everything.

[![CI](https://github.com/KirillDomitin/Tollwire/actions/workflows/ci.yml/badge.svg)](https://github.com/KirillDomitin/Tollwire/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-2b6b4f.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab)
![Node 24](https://img.shields.io/badge/node-24-5fa04e)
![FastAPI](https://img.shields.io/badge/FastAPI-009688)
![React 19](https://img.shields.io/badge/React-19-61dafb)

English · [Русский](README.ru.md)

</div>

---

## Why WirePilot

Running a handful of VPN servers by hand means SSH sessions, copied configs and no record of who has access to what. WirePilot puts that behind one web panel and one REST API:

- **You connect a server once.** The panel pins its SSH host key, installs the VPN in Docker and keeps the SSH credentials encrypted.
- **You give people access, not files.** A person gets a subscription with an expiry date; the panel creates, rotates and revokes their clients on the servers for you.
- **You always know what happened.** Every action by an admin lands in an audit log; every long operation runs as a tracked background job.

## Features

| | |
|---|---|
| **Servers** | Add a server, pin its SSH host key, install the VPN, check health, re-sync with what is actually running. Rename it, add a note, give it a country flag (🇩🇪 `frankfurt-1`). |
| **VPN users and access** | Users with subscriptions and expiry dates; grants per server; instant revoke. Expired access is switched off automatically. |
| **Client configs** | `vpn://` link, a series of QR codes and a `.conf` file. Revealing a config needs its own permission, because it contains the client's private key. |
| **Monitoring** | CPU, memory, disk, network and clients online per server, collected over SSH (read-only). Charts for 1 hour, 24 hours, 7 and 30 days; the servers table shows the latest values. |
| **Background jobs** | Installation, config issue and revoke, health checks and sync run in a worker with a Redis queue and a lock per server, so two operations never touch the same machine at once. Lost and stuck jobs are recovered automatically. |
| **Audit log** | Who did what, to which server or user, and when. Filter by admin, event group, object and date. |
| **Roles** | `viewer` (read only), `operator` (users, access, configs, checks), `superadmin` (infrastructure and admins). Buttons the role cannot use are hidden. |
| **Languages** | The panel speaks English and Russian: a switch in the sidebar, remembered in the browser, defaulting to the browser's language. Dates, numbers and country names follow it. |
| **Security** | Secrets encrypted in the database (Fernet); SSH only to servers with a pinned host key; the panel can replace a password with its own SSH key and then forget the password; login rate limiting and lockout; refresh-token rotation; no user input in SSH commands; no secrets in logs. |

## Architecture

```
 browser ──► nginx ──► API (FastAPI) ──► PostgreSQL
              │            │
        static React       └──► Redis ◄── worker ──► SSH ──► your VPN servers
        frontend                          (jobs + scheduler)
```

- **API** — FastAPI, layered as router → service → repository / VPN provider. The VPN protocol sits behind a `VPNProvider` interface, so business logic knows nothing about SSH, Docker or config formats.
- **Worker** — runs queued jobs and a scheduler (health checks, sync, monitoring, access expiry, retention).
- **Frontend** — React 19 + Vite + TypeScript, with typed API calls generated from the backend's OpenAPI schema.
- **Data** — PostgreSQL for everything durable, Redis for the job queue and rate limits.

Each folder under `src/` has a short `CLAUDE.md` describing its module.

## Quick start (development)

You need Docker, Python 3.12+ with [uv](https://docs.astral.sh/uv/), and Node.js 24.

```bash
git clone <repository-url> tollwire && cd tollwire
uv sync && (cd frontend && npm ci)

docker compose up -d                 # PostgreSQL + Redis
cp .env.example .env                 # then set JWT_SECRET_KEY and ENCRYPTION_KEY (see INSTALL.md)
uv run alembic upgrade head
PYTHONPATH=src uv run python -m scripts.create_admin you@example.com

uv run uvicorn main:app --app-dir src --reload      # API    → http://localhost:8000
uv run python src/worker.py                          # worker
(cd frontend && npm run dev)                         # panel  → http://localhost:5173
```

Full instructions, including PowerShell and troubleshooting, are in [INSTALL.md](INSTALL.md).

## Production

Everything runs in Docker behind nginx with TLS: `docker-compose.prod.yml` starts nginx, the API, the worker, a one-shot migration job, PostgreSQL and Redis. Only nginx is exposed; the database and Redis sit on an internal network. The quickest way is `./install.sh --domain panel.example.com`: it creates the secrets, issues a TLS certificate, starts the panel and offers to create the first administrator. See the [production section of INSTALL.md](INSTALL.md#production) for the details, certificates, updates and backups.

> **Back up `ENCRYPTION_KEY` together with the database.** SSH credentials and VPN configs are encrypted with it. Without the key a database backup cannot be read.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest                                # needs `docker compose up -d`

cd frontend
npm run typecheck && npm run lint && npm test && npm run build
```

If you change the API, regenerate the frontend types:

```bash
PYTHONPATH=src uv run python -m scripts.export_openapi frontend/openapi.json
cd frontend && npm run gen:api
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and [CHANGELOG.md](CHANGELOG.md) for what changed.

## Project layout

```
src/
  core/            shared: config, database, security, errors
  infrastructure/  the outside world: ssh, vpn providers, workers, monitoring
  modules/         domain: auth, servers, protocols, vpn_users, access, jobs, audit, monitoring
  scripts/         create_admin, export_openapi
alembic/           database migrations
frontend/          React panel
deploy/nginx/      production nginx config
tests/             unit, service and integration tests
```

## Security

Found a vulnerability? Please do not open a public issue — see [SECURITY.md](SECURITY.md).

## Disclaimer

WirePilot is an independent project. It is not affiliated with, endorsed by or sponsored by Amnezia or the AmneziaVPN project. It manages servers that you own or are authorised to administer.

## License

[MIT](LICENSE) © 2026 Kirill Domitin
