# scripts — консольные утилиты

Запуск из корня репозитория: `PYTHONPATH=src uv run python -m scripts.<имя>` (в проде —
`docker compose ... run --rm api python -m scripts.<имя>`).

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
