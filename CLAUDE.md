# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# VPN Control Panel (WirePilot)

REST API + веб-интерфейс (продукт называется WirePilot; интерфейс только тёмный, сайдбар закреплён) для управления VPN-серверами AmneziaWG: серверы, VPN-пользователи, доступы, выдача
конфигов, установка по SSH, фоновые задачи, мониторинг, аудит. Детали — в `CLAUDE.md` каждой папки; запуск и
деплой — `INSTALL.md`.

## Как работать

- Фича: `architect` → решение пользователя → `tester` (красные тесты) → реализация → `reviewer` → исправления →
  `doc-writer` → коммит. Агенты — `.claude/agents/`.
- Архитектурную развилку — на решение пользователя до кода, с рекомендацией.
- После изменений: ruff, mypy, тесты, миграции (upgrade/downgrade) и живой прогон.
- Изменения на реальных серверах — только с явного согласия пользователя.
- Поменялось устройство модуля — обновить его `CLAUDE.md`.
- Коммиты без Co-Authored-By. Документация библиотек — через Context7.

## Архитектура

- Слои сверху вниз: router → service → repository / `VPNProvider` → SSH/БД. В роутерах только HTTP, схемы,
  `require(Permission.X)` и вызов сервиса.
- VPN — только через `VPNProvider`; бизнес-логика не знает SSH, Docker и форматов конфигов.
- `infrastructure/` не импортирует `modules/` (кроме воркера и планировщика).
- Доменные ошибки — `core/exceptions.py`, в HTTP-коды их переводит `main.py`.

## Безопасность

Секреты (SSH, VPN-конфиги) — в БД только зашифрованными; `ENCRYPTION_KEY` и `.env*` — не читать, не коммитить.
Никакого пользовательского ввода в SSH-команды. В логи — никаких паролей, ключей, токенов, конфигов.

## Где что

`src/core` общее · `src/infrastructure/{ssh,vpn,workers,monitoring}` внешний мир · `src/modules/*` домен
(auth, servers, protocols, vpn_users, access, jobs, audit, monitoring) · `src/scripts` утилиты · `alembic` миграции
· `tests` · `frontend` интерфейс.

## Команды

```bash
docker compose up -d                             # Postgres + Redis (нужны API, воркеру и тестам)
uv run uvicorn main:app --app-dir src --reload   # API
uv run python src/worker.py                      # воркер + планировщик
uv run ruff check . && uv run mypy src && uv run pytest
uv run pytest tests/unit/test_x.py::test_y       # один тест
uv run alembic upgrade head / downgrade -1       # миграции
```

Фронтенд (`frontend/`): `npm run dev`, проверки — `npm run typecheck && npm run lint && npm test`.

Прод: `./install.sh` (установка одной командой), `./panel backup|restore|update|logs` (обслуживание); общий код
скриптов — `deploy/common.sh`. Тесты — `tests/unit/test_install_*.py` и `test_panel.py` (подробности в `tests/CLAUDE.md`).
