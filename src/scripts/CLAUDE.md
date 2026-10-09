# scripts — консольные утилиты

Запуск из корня репозитория: `PYTHONPATH=src uv run python -m scripts.<имя>` (в проде —
`docker compose ... run --rm api python -m scripts.<имя>`).

- `create_admin <email>` — создать админа; пароль спрашивается через `getpass` (не попадает в историю shell).
  На Windows getpass не читает pipe — в скриптах проверок создавать админа кодом (`create_admin()`), а после
  проверки удалять.
- `export_openapi <путь>` — выгрузить схему API без запуска сервера; для фронта:
  `export_openapi frontend/openapi.json`, затем `npm run gen:api` в `frontend/`. Запускать после любого изменения
  схем или маршрутов.
