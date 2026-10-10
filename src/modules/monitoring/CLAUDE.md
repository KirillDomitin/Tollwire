# modules/monitoring — метрики серверов

Сбор без агента: раз в минуту одна фиксированная команда только для чтения по SSH. Команда и разбор вывода —
`src/infrastructure/monitoring/` (свой `CLAUDE.md`); здесь — хранение, сбор по серверам и API.

## Хранение

- `server_metrics` (сырые, PK `(server_id, ts)`, индекс по `ts`) — 7 дней (`METRICS_RAW_RETENTION_DAYS`).
- `server_metrics_hourly` (PK `(server_id, hour)`) — `<метрика>_avg` и `_max`, 90 дней.
- Свёртка `MetricsRepository.rollup`: `INSERT … SELECT … ON CONFLICT DO UPDATE` за последние 3 часа (идемпотентна).

## Сервис (`service.py`)

- `MetricsCollector.collect_all` — серверы с учёткой, параллельно не больше `METRICS_CONCURRENCY`; ошибки SSH и
  разбора → пропуск сервера, не падение правила. Клиенты: `provider.client_counts()` (всего / онлайн), если на
  сервере есть протокол. Тип протокола и прошлые счётчики снимать **до** `rollback` (иначе `DetachedInstanceError`).
- `MetricsService.series(range)` — 1h/24h из сырых (поминутно), 7d/30d из почасовых (среднее + пик);
  `latest()` — последний замер каждого сервера (DISTINCT ON).
- Правила планировщика `metrics` (60 с) и `metrics_rollup` (300 с) — `rollup_and_prune`.

## API

`GET /api/v1/servers/{id}/monitoring?range=1h|24h|7d|30d` → `MetricsSeries` (`resolution` minute/hour, `points`),
`GET /api/v1/monitoring/latest` → `list[LatestMetrics]`. Право `servers:read`; неизвестный сервер → 404.

## Фронтенд

`frontend/src/pages/server/MonitoringSection.tsx` (графики uPlot, ленивая загрузка), колонки «Клиенты»/«CPU» в
`ServersPage.tsx` (замер старше 5 минут показывается как «—»). Подробнее — `frontend/CLAUDE.md`.

## Расширение

Онлайн и трафик по каждому клиенту можно брать из `awg show dump` — там рукопожатия и байты по peer'ам.
