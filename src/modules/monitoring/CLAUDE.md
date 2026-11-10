# modules/monitoring — метрики серверов

Сбор без агента: раз в минуту фиксированные команды только для чтения по SSH. Метрики хоста и разбор вывода —
`src/infrastructure/monitoring/` (свой `CLAUDE.md`); статистика peer'ов — через `VPNProvider.client_stats()`.
Здесь — хранение, сбор по серверам и API.

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
- `client_metrics.py`: текущие peer-счётчики в Redis, отдельно от исторических метрик хоста. Сборщик сохраняет
  только успешный снимок; сбой не подменяет предыдущий замер нулями. `client_counts()` остаётся отдельным.

## API

`GET /api/v1/servers/{id}/monitoring?range=1h|24h|7d|30d` → `MetricsSeries` (`resolution` minute/hour, `points`),
`GET /api/v1/monitoring/latest` → `list[LatestMetrics]`. Право `servers:read`; неизвестный сервер → 404.

`GET /api/v1/servers/{id}/clients/monitoring` → `ServerClientMetrics`, право `servers:read`, только БД/Redis,
без SSH. Сопоставление — протокол + публичный ключ живого credential, не имя; скачано = TX сервера,
отправлено = RX сервера. Контракт, свежесть и ограничения — [статистика клиентов](../../../docs/monitoring/client-metrics.md).

## Фронтенд

`frontend/src/pages/server/MonitoringSection.tsx` (графики uPlot, ленивая загрузка), колонки «Клиенты»/«CPU» в
`ServersPage.tsx` (замер старше 5 минут показывается как «—»). Подробнее — `frontend/CLAUDE.md`.

Статистика устройства в `pages/user/ClientMetrics.tsx`: общий запрос на сервер раз в 60 с, только для живого
credential и с `servers:read`. Нет данных не означает нулевой трафик или отсутствие рукопожатий.
