# modules/jobs — фоновые задачи

Всё, что идёт по SSH и меняет сервер, — задача: API создаёт `Job` и отвечает 202, воркер
(`infrastructure/workers/`) выполняет.

## Модель `jobs`

- `type` (`JobType`), `status` (`JobStatus`: pending/running/success/failed/cancelled), `progress`,
  `started_at`/`finished_at`, `error` (текст, redact + обрезка), `result` (JSONB — отчёты sync/health/ssh_key/discover).
- `server_id` (FK SET NULL — история переживает удаление сервера), `access_grant_id` (FK SET NULL),
  `admin_id` (инициатор, FK `jobs_admin_id_fkey`) — от его имени воркер пишет аудит.
- Типы в работе: CREATE_USER, REVOKE_ACCESS, SYNC_SERVER, HEALTH_CHECK, INSTALL_SERVER, INSTALL_PROTOCOL,
  REMOVE_PROTOCOL, INSTALL_SSH_KEY, DISCOVER_VPN (только чтение, `result` = `{"protocols": [...]}`). DELETE_USER и UNINSTALL_SERVER — наследие, не ставятся.

## Очередь (`queue.py`)

Redis LIST `jobs:queue`: LPUSH + BRPOP = FIFO, в очереди только `job_id`. `JobService.create` атомарно: Job в БД
(commit), затем enqueue; потерянные PENDING возвращает правило `requeue_jobs`.

## Сервис

- `create`, `get`, `list_all` (фильтры `server_id`, `status`; новые первыми; limit ≤ 100).
- `has_unfinished_for_server/grant` (репозиторий) — не ставить дубль, пока такая же задача не завершена.
- `fail_stale_running` (RUNNING > 30 мин → FAILED), `requeue_lost_pending`, `purge_finished`.
- `record_finished` пишет аудит started/completed/failed по таблицам `_STARTED/_COMPLETED/_FAILED`. Для INSTALL_SSH_KEY
  результат задачи (fingerprint, username, replaced, removed_old) целиком попадает в meta аудита. Для DISCOVER_VPN в meta — только счётчики `protocols` и `clients`.

## API

`GET /api/v1/jobs` (`?server_id=&status=` — enum по **значению**: `?status=failed`, не `FAILED`),
`GET /api/v1/jobs/{id}`. Право `jobs:read`. Фильтра по гранту нет — фронт следит за задачами по их id.

Зависший старый процесс воркера забирает задачи и выполняет их старым кодом. Проверить:
`docker compose exec redis redis-cli CLIENT LIST | grep brpop`.
