# infrastructure/workers — фоновый воркер и планировщик

Процесс воркера стартует из `src/worker.py`: `JobWorker.run()` и `Scheduler.run()` в одном `asyncio.gather`,
мягкая остановка по SIGINT/SIGTERM. Воркер и планировщик — composition root (точка сборки): им можно
импортировать `modules/`.

## JobWorker (`worker.py`)

- Берёт `job_id` из Redis-очереди (`modules/jobs/queue.py`, BRPOP), читает Job `with_for_update`, ведёт статусы
  PENDING → RUNNING → SUCCESS/FAILED. Одна сессия БД на задачу; любая ошибка → FAILED с текстом
  (`job_error_text`: redact + обрезка), цикл живёт дальше.
- Диспетчер `match JobType`: `CREATE_USER`, `REVOKE_ACCESS`, `SYNC_SERVER`, `HEALTH_CHECK`, `INSTALL_SERVER`,
  `INSTALL_PROTOCOL`, `REMOVE_PROTOCOL`, `INSTALL_SSH_KEY`, `DISCOVER_VPN`, `GATEWAY_ADD_EXIT` / `GATEWAY_REMOVE_EXIT` / `GATEWAY_APPLY_RU` (под `ServerLock` шлюза), `GATEWAY_APPLY` (failover и ручной перенос шлюза, под `ServerLock` шлюза), `GEOIP_UPDATE` (глобальная: без `server_id` и блокировки) (`DELETE_USER` — заглушка;`IMPORT_CLIENTS` (`DELETE_USER` — заглушка; `UNINSTALL_SERVER` не
  обрабатывается → явный FAILED). Серверная задача без `server_id` → FAILED (`_reject`).
- `DISCOVER_VPN` — без ServerLock, только чтение (`ServerDiscoveryService.discover`). `INSTALL_SERVER` после
  установки делает тот же поиск внутри своей задачи: сбой поиска логируется (warning), установка остаётся SUCCESS.
- `IMPORT_CLIENTS` — под `ServerLock`: `ClientImportService.run(job)` (читает сервер, пишет в БД в той же
  сессии). Commit делает воркер уже после снятия блокировки (статус задачи и данные импорта — одной
  транзакцией). Окно между снятием и commit известно и безвредно: новый импорт на этом сервере не ставится,
  пока задача не завершена (`has_unfinished_for_server`).
- Серверные операции — под `ServerLock`; сервер занят → задача обратно в PENDING и в очередь с паузой.
- Перед записью FAILED — `rollback` и перечитать Job (сессия могла сломаться).
- Сбой Redis → backoff, не падение процесса. Логи с `bind(job_id, job_type, server_id)`.
- Тесты: `JobWorker.process_next()` обрабатывает одну задачу — удобно в интеграционных тестах (`drain(worker)`).

## ServerLock (`locks.py`)

Redis lock `lock:server:{id}`, TTL = `STALE_JOB_TIMEOUT_SECONDS` (не меньше самой долгой задачи, установка до
~10 мин), `blocking_timeout` 5 с. Не взяли → `ServerBusyError`. Истёк до release → warning, а не FAILED.

## Scheduler (`scheduler.py`)

Тик `SCHEDULER_TICK_SECONDS`; правило выполняется, если взят Redis-ключ `scheduler:rule:<name>` (SET NX EX
interval) — несколько воркеров не дублируют работу. Ошибка правила логируется, планировщик живёт.
`SCHEDULER_ENABLED=false` — выключить (так в тестах).

| Правило | Что делает |
|---|---|
| `access` | активация и истечение подписок (своя сессия на подписку), при истечении — задачи отзыва клиентов |
| `admin_sessions` | чистка истёкших refresh-сессий |
| `stale_jobs` | RUNNING дольше 30 мин → FAILED (иначе блокировали бы плановые проверки навсегда) |
| `requeue_jobs` | PENDING, потерянные очередью → снова в очередь |
| `health` | задачи HEALTH_CHECK всем серверам с учёткой (кроме INSTALLING) |
| `sync` | задачи SYNC_SERVER серверам с протоколом — **меняет** серверы (удаляет «сирот») |
| `retention` | удаление старых задач и аудита |
| `geoip` | раз в неделю задача `GEOIP_UPDATE`, только если на панели есть шлюз (нет шлюзов - пропуск; незавершённая такая же - пропуск) |
| `gateway_watch` | раз в минуту: чтение туннелей выходов шлюзов ПАРАЛЛЕЛЬНО (до 5, не дольше 20 с на шлюз; SSH, только чтение), счётчики отказов; при отказе / восстановлении / переносах - задача `GATEWAY_APPLY` (без дублей). Ошибка или срок одного шлюза остальные не задерживают и счётчики не меняют |
| `metrics` | замер серверов по SSH (только чтение) — прямо в планировщике, не задачей |
| `metrics_rollup` | почасовая свёртка метрик и чистка старых |

⚠️ Если один VPN-сервер подключён к двум панелям (dev и prod), dev-воркер не запускать: правило `sync` каждой
панели удаляет клиентов другой (имена `grant-*`).
