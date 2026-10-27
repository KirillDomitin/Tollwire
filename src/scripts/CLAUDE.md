# scripts — консольные утилиты

Запуск из корня репозитория: `PYTHONPATH=src uv run python -m scripts.<имя>` (в проде —
`docker compose ... run --rm api python -m scripts.<имя>`). Каталог — пакет (`__init__.py`). Общее для скриптов бота —
`cli_common.py`: `Console` (терминал и stdin, подменяется в тестах), `CliError` (отказ без трассировки), язык сообщений
(`PANEL_LANG`, иначе локаль) и `translator`.

- `create_admin [email]` — создать админа. Меню: email, затем пароль — случайный (по умолчанию; `must_change_password`,
  виден один раз, **только в терминале** — при выводе не в TTY скрипт отказывается до создания) или свой (`getpass`,
  ≥12 символов). `--random-password` — без вопросов; `--only-if-empty` — не делать ничего, если админы уже есть
  (повторный запуск установщика); `--role`. Пишет `admin_created` в аудит (`source: cli`, без пароля). Логика —
  `run(argv, Console)`, терминал подменяется в тестах (`tests/integration/test_create_admin_cli.py`). На Windows
  getpass не читает pipe — в скриптах проверок создавать админа кодом (`create_admin(email, password, role)`), а
  после проверки удалять.
- `export_openapi <путь>` — выгрузить схему API без запуска сервера; для фронта:
  `export_openapi frontend/openapi.json`, затем `npm run gen:api` в `frontend/`. Запускать после любого изменения
  схем или маршрутов.
- `bot_key` — ключи Telegram-бота: `create --name N [--raw]`, `revoke` (ключ из stdin), `list`, `seen --within S`
  (ключ из stdin; код выхода 0, если бот на связи). Ключ показывается один раз и только в терминале (`--raw` — только
  ключ, для установщика); без терминала и без `--raw` отказ до выпуска. Выпуск и отзыв — `BotKeyService` с
  `source: cli` в аудите. API ключей требует JWT, поэтому установщик и `./panel bot` работают через этот скрипт.
- `bot_invite` — приглашения в бота: `protocols` (id протоколов), `create --protocol ID --days N --expires-in 7d|12h
  [--note] [--raw]` → ссылка `t.me/<бот>?start=<код>` (код, пока бот не выходил на связь). То же правило терминала, что у
  `bot_key`; аудит `source: cli`, без кода.
- Запуск через панель: `./panel bot ...` (`deploy/bot.sh`) или напрямую
  `docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.bot_key …`.
