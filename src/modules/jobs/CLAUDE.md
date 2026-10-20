# modules/jobs — фоновые задачи

Всё, что идёт по SSH и меняет сервер, — задача: API создаёт `Job` и отвечает 202, воркер
(`infrastructure/workers/`) выполняет.

## Модель `jobs`

- `type` (`JobType`), `status` (`JobStatus`: pending/running/success/failed/cancelled), `progress`,
  `started_at`/`finished_at`, `error` (текст, redact + обрезка), `result` (JSONB — отчёты sync/health/ssh_key/discover/импорта),
  `params` (JSONB — входные данные задачи, если нужны: для импорта — протокол, `valid_until`, клиенты с ключами,
  именами, email и `user_id`; PSK туда не попадает). У `INSTALL_PROTOCOL`/`REMOVE_PROTOCOL` `params` = `{"protocol_type": "amnezia_gateway"}` для шлюза и
  отсутствует для обычного протокола (воркер читает отсутствие как `amnezia_wg`; неизвестный тип или тип
  приложения - задача FAILED). `params` в `JobRead` не отдаётся; поле в БД открытым текстом.
- `server_id` (FK SET NULL — история переживает удаление сервера), `access_grant_id` (FK SET NULL),
  `admin_id` (инициатор, FK `jobs_admin_id_fkey`) — от его имени воркер пишет аудит.
- Типы в работе: CREATE_USER, REVOKE_ACCESS, SYNC_SERVER, HEALTH_CHECK, INSTALL_SERVER, INSTALL_PROTOCOL,
  REMOVE_PROTOCOL, INSTALL_SSH_KEY, DISCOVER_VPN (только чтение, `result` = `{"protocols": [...]}`), IMPORT_CLIENTS (`params` — вход, `result` —
  итог импорта), GATEWAY_ADD_EXIT / GATEWAY_REMOVE_EXIT (выходы шлюза, этап 24.2: `server_id` — сервер-шлюз, `params` — `exit_id`,
  `exit_server_id`, `slot` (и `weight` у добавления), `result` — `{exit_id, exit_server_id, slot}` / `{…, peer_revoked}`; воркер
  держит `ServerLock` шлюза, выход блокирует сервис внутри; занятый выход — задача в очередь, как `ServerBusyError`). DELETE_USER и UNINSTALL_SERVER — наследие, не ставятся. GEOIP_UPDATE (этап 24.4, глобальная: `server_id` нет, ServerLock не берётся; `result` - итог приёма списка) и
  GATEWAY_APPLY_RU (`server_id` — шлюз, без `params`; `result` — `{routes, table, prefixes}`; воркер держит `ServerLock` шлюза;
  в аудите `gateway_ru_applied` с числом маршрутов). Дубли: `has_unfinished_of_type` (GEOIP_UPDATE) и `has_unfinished_for_server`
  (GATEWAY_APPLY_RU).
- `result` `IMPORT_CLIENTS`: `{protocol_id, imported: [{public_key, user_id, credential_id}], already: [public_key],
  gone: [public_key], unsupported: [public_key], conflict: [{public_key, reason}]}`. Секретов нет: ключи публичные,
  `reason` — фиксированный текст без параметров SQL.

## Очередь (`queue.py`)

Redis LIST `jobs:queue`: LPUSH + BRPOP = FIFO, в очереди только `job_id`. `JobService.create` атомарно: Job в БД
(commit), затем enqueue; потерянные PENDING возвращает правило `requeue_jobs`.

## Сервис

- `create(type, server_id, access_grant_id, params)` — `params` пишется в `Job.params`; `get`, `list_all`
  (фильтры `server_id`, `status`; новые первыми; limit ≤ 100).
- `has_unfinished_for_server/grant` (репозиторий) — не ставить дубль, пока такая же задача не завершена.
- `fail_stale_running` (RUNNING > 30 мин → FAILED), `requeue_lost_pending`, `purge_finished`.
- Аудит `protocol_install_*` / `protocol_remove_*` (started, completed, failed) несёт в meta `protocol_type` - значение
enum из `params` (без params - `amnezia_wg`; нераспознанное не пишется). `record_finished` пишет аудит started/completed/failed по таблицам `_STARTED/_COMPLETED/_FAILED`. Для INSTALL_SSH_KEY
  результат задачи (fingerprint, username, replaced, removed_old) целиком попадает в meta аудита. Для DISCOVER_VPN в meta — только счётчики `protocols` и `clients`. Для GATEWAY_REMOVE_EXIT к ним добавлены счётчики `moved` / `unassigned` из результата (клиентов перенесено и оставлено без выхода). Для GATEWAY_*_EXIT — `exit_id`, `exit_server_id`, `slot`, `weight` из `params` (ключей нет). Для IMPORT_CLIENTS — `protocol_id` и счётчики
  `imported`, `already`, `gone`, `unsupported`, `conflict` (ключей и имён в журнал не попадают).

## API

`GET /api/v1/jobs` (`?server_id=&status=` — enum по **значению**: `?status=failed`, не `FAILED`),
`GET /api/v1/jobs/{id}`. Право `jobs:read`. Фильтра по гранту нет — фронт следит за задачами по их id.

Зависший старый процесс воркера забирает задачи и выполняет их старым кодом. Проверить:
`docker compose exec redis redis-cli CLIENT LIST | grep brpop`.
