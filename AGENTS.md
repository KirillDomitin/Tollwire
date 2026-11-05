# AGENTS.md

This file provides guidance to Codex (claude.ai/code) when working with code in this repository.

# VPN Control Panel

REST API + веб-интерфейс для управления VPN-серверами AmneziaWG: серверы, VPN-пользователи, доступы, выдача
конфигов, установка по SSH, фоновые задачи, мониторинг, аудит. Детали — в `AGENTS.md` каждой папки; запуск и
деплой — `INSTALL.md`.

## Как работать

- Мелкая правка с понятной причиной: без агентов/плана, `/quick` по желанию.
- Обычная задача: существующий механизм → реализация → затронутые тесты → ревью;
  reviewer отдельно при изменениях поведения на нескольких слоях.
- `/feature` — по команде либо для новых механизмов БД, VPNProvider/SSH, безопасности,
  существенного API-контракта: architect → решение → tester → реализация → reviewer.
  Число файлов само по себе не критерий. Архитектурную развилку решает пользователь до кода.
- Зафиксировать исходный HEAD и локальные изменения; ревью/документация только по задаче.
  Читать через rg/outline нужные символы, локальные инструкции и справочники по теме;
  без AGENTS.md использовать CLAUDE.md. Не перечитывать неизменённое.
- Агентам — цель, файлы/диапазон, инварианты, критерии; для узких задач свежий контекст,
  если возможно. Ответ — результат/находки/проверки, без пересказа кода.
- Python: ruff/mypy и конкретные тесты либо testmon; frontend: typecheck/lint и затронутые
  тесты; bot: своё окружение; текст: diff/ссылки. В /quick запуск напрямую, в /feature —
  test-runner, в обычном режиме — напрямую либо один test-runner.
- Полный прогон один раз в конце /feature и при изменениях install.sh/panel/deploy/alembic;
  иначе по риску общей регрессии. После правок повторять затронутые проверки;
  полный повтор при изменении общего механизма/оставшемся риске. Ревьюер не дублирует запуски.
- Upgrade/downgrade при изменениях миграций; живой прогон изменённого runtime-сценария.
  Реальные серверы — только с явного согласия; непроверенное отметить в итоге.
- Документация только изменённых контрактов; doc-writer при существенных изменениях нескольких
  документов. Инструкции — инварианты/грабли до ~8 КБ, подробности в docs/<модуль>/ по ссылке.
- Коммиты по поручению/плану, без Co-Authored-By. Библиотечные API — через Context7 по нужному
  вопросу; не повторять запрос без новой неясности.

## Архитектура

- Слои сверху вниз: router → service → repository / `VPNProvider` → SSH/БД. В роутерах только HTTP, схемы,
  `require(Permission.X)` и вызов сервиса.
- VPN — только через `VPNProvider`; бизнес-логика не знает SSH, Docker и форматов конфигов.
- `infrastructure/` не импортирует `modules/` (кроме воркера и планировщика).
- Доменные ошибки — `core/exceptions.py`, в HTTP-коды их переводит `main.py`.
- `bot/` не импортирует `src/`: к панели только через Bot API (`tests/unit/test_bot_isolation.py`).

## Безопасность

Секреты (SSH, VPN-конфиги) — в БД только зашифрованными; `ENCRYPTION_KEY` и `.env*` — не читать, не коммитить.
Никакого пользовательского ввода в SSH-команды. В логи — никаких паролей, ключей, токенов, конфигов.
Токен Telegram и ключ бота — только в `.env.bot` (сервер панели) или `bot/.env` (0600), не в `.env.prod`.

## Где что

`src/core` общее · `src/infrastructure/{ssh,vpn,workers,monitoring}` внешний мир · `src/modules/*` домен
(auth, servers, protocols, vpn_users, access, jobs, audit, monitoring, bot) · `src/scripts` утилиты (в т.ч. ключи и
приглашения бота) · `bot` Telegram-бот, отдельный пакет (`bot/AGENTS.md`) · `alembic` миграции · `tests` · `frontend` интерфейс.

## Команды

```bash
docker compose up -d                             # Postgres + Redis (нужны API, воркеру и тестам)
uv run uvicorn main:app --app-dir src --reload   # API
uv run python src/worker.py                      # воркер + планировщик
uv run ruff check . && uv run mypy src
uv run pytest --testmon -q -x -m "not slow"     # только тесты, затронутые изменениями (обычный режим)
uv run pytest -n 4 -q -m "not slow"              # быстрый полный прогон (~2900 тестов, ~4 мин) — в фоне или через test-runner
uv run pytest -n 4 -q                             # всё, ~3400 тестов, ~12+ мин: перед коммитом, если менялись install.sh/panel/deploy/alembic
uv run pytest tests/unit/test_x.py::test_y       # один тест
uv run alembic upgrade head / downgrade -1       # миграции
uv run --project bot pytest                      # тесты бота (своё окружение bot/; проверки как в CI — из bot/)
```

Фронтенд (`frontend/`): `npm run dev`, проверки — `npm run typecheck && npm run lint && npm test`.

Прод: `./install.sh` (установка одной командой; `--bot local|remote|none` — Telegram-бот), `./panel backup|restore|update|logs|cert|bot`
(обслуживание и бот); общий код скриптов — `deploy/common.sh`, `deploy/bot.sh`. Нужен Docker Compose 2.24+.
Тесты установщика и бота — `tests/unit/test_install_*.py`, `test_panel*.py`, `bot/tests` (подробности в `tests/AGENTS.md`).
