# alembic — миграции

- Async-настройка в `env.py`; **каждый модуль с моделями импортируется в `env.py`**, иначе autogenerate его не
  увидит.
- Новая миграция: `uv run alembic revision --autogenerate -m "..."` → прочитать и поправить руками →
  `upgrade head` → `downgrade -1` → `upgrade head` (проверка в тестах: `tests/services/test_migrations.py`).
- Правишь модель до коммита — перегенерируй ту же миграцию, а не добавляй вторую поверх.

## Грабли Postgres enum (ловили трижды)

- Enum-тип в Postgres — отдельный объект БД.
  - `CREATE TABLE` создаёт тип сам, а `add_column` на существующую таблицу — нет:
    `sa.Enum(..., name="x").create(op.get_bind(), checkfirst=True)` перед `add_column`.
  - `DROP TABLE` тип не удаляет: в `downgrade()` — `sa.Enum(name="x").drop(op.get_bind(), checkfirst=True)`.
- Не хочешь миграций на каждое новое значение — `Enum(native_enum=False)` (VARCHAR), как у `audit_logs.action`.
- Безымянные ограничения называть явно (`server_credentials_server_id_key`, `jobs_admin_id_fkey`), иначе
  downgrade не найдёт их по имени.
- Удалил файл применённой миграции → `alembic current` скажет «Can't locate revision». Сначала сверить
  `alembic_version` с файлами, руками ничего не чинить.

## Новые значения enum (`d3a7c9e15b42`, поиск Amnezia)

- `ADD VALUE` для `protocoltype` (`AMNEZIA_APP_AWG`, `AMNEZIA_APP_AWG2`) и `jobtype` (`DISCOVER_VPN`) — в
  `op.get_context().autocommit_block()`, с `IF NOT EXISTS`.
- Downgrade удаляет строки `protocols` с новыми типами и `jobs` с `DISCOVER_VPN`, затем пересоздаёт оба enum без
  новых значений. Значение из enum Postgres иначе не убрать.

## Шлюз (`a8c4e1f7b925`)

- `protocoltype` += `AMNEZIA_GATEWAY` - в `autocommit_block`, с `IF NOT EXISTS`.
- Downgrade удаляет строки `protocols` шлюза (каскадом уходят их гранты и credential'ы) и пересоздаёт enum из трёх
  прежних значений (как `d3a7c9e15b42`); уникальность `(server_id, type)` сохраняется.

## Выходы шлюза (`c7e1b4d8a932`)

- `jobtype` += `GATEWAY_ADD_EXIT`, `GATEWAY_REMOVE_EXIT` — в `autocommit_block`, с `IF NOT EXISTS`.
- Таблица `gateway_exits` (FK на `protocols`: шлюз CASCADE, выход RESTRICT; UNIQUE `(шлюз, слот)` и `(шлюз, выход)`; CHECK слота
  1..200, веса 1..100, `max_clients > 0`; `state` — VARCHAR(16), не Postgres-enum; индекс по `exit_protocol_id`).
- Downgrade: удаляет таблицу, задачи этих типов и пересоздаёт `jobtype` (DEFAULT `HEALTH_CHECK` возвращается).

## Назначения выходов (`d2f8a6c3b514`)

- Таблица `gateway_assignments`: PK `credential_id` (FK `vpn_credentials` CASCADE), `exit_id` (FK `gateway_exits` CASCADE, индекс),
  `pinned_exit_id` (FK `gateway_exits` SET NULL), `assigned_at`, `reason` (VARCHAR(16), не Postgres-enum).
- Также `gateway_exits.drained_moved` / `drained_unassigned` (INTEGER NOT NULL DEFAULT 0): итог переноса клиентов при снятии выхода.
- Downgrade удаляет эти колонки и таблицу (производные данные: карту на шлюзе панель собирает из БД).

## «Россия напрямую» (`e5a1c9f3b207`)

- `jobtype` += `GEOIP_UPDATE`, `GATEWAY_APPLY_RU` (в `autocommit_block`, `IF NOT EXISTS`). Таблицы `geoip_lists` (статус -
  VARCHAR(16), не Postgres-enum; частичный уникальный индекс `uq_geoip_lists_one_active` по `status = 'ACTIVE'`) и
  `geoip_overrides` (UNIQUE `uq_geoip_overrides_network`; `action` - VARCHAR(16)).
- Downgrade удаляет таблицы (списки скачиваются заново), задачи двух новых типов и пересоздаёт `jobtype` из прежних значений
  с `DEFAULT 'HEALTH_CHECK'` (как миграции выходов).

## Failover выходов (`f3b8d2a6c914`)

- `gateway_exits.consecutive_failures` (INTEGER NOT NULL DEFAULT 0), `healthy_since` (timestamptz, NULL) и `failure_notified`
  (BOOLEAN NOT NULL DEFAULT false: событие `gateway_failover` за отказ уже записано); `jobtype` +=
  `GATEWAY_APPLY` (`autocommit_block`, `IF NOT EXISTS`). Значения `AssignReason` (`failover_return`) - VARCHAR(16), миграции не нужно.
- Downgrade удаляет колонки (счётчики производные), задачи `GATEWAY_APPLY` и пересоздаёт `jobtype` с `DEFAULT 'HEALTH_CHECK'`.

## Импорт клиентов (`f6b2d8a41c73`)

- `jobtype` += `IMPORT_CLIENTS` — в `autocommit_block`, с `IF NOT EXISTS`.
- `vpn_users.email` → NULL. `vpn_credentials`: `encrypted_config` → NULL; `source` (VARCHAR(16), не
  Postgres-enum; `server_default 'ISSUED'` — старые строки становятся ISSUED), `encrypted_psk`,
  `external_name`; CHECK `ck_vpn_credentials_issued_has_config`. `jobs.params` (JSONB, NULL).
- Downgrade: удаляет гранты, у которых остались только `IMPORTED` credential'ы (вместе с ними), затем сами
  `IMPORTED` credential'ы и задачи `IMPORT_CLIENTS`; пользователям без email ставит `<id>@imported.invalid`;
  дальше возвращает NOT NULL и пересоздаёт `jobtype` без нового значения. Гранты с обычным credential'ом,
  пользователи, подписки и peer'ы на сервере не трогаются.

## Telegram-бот (`a7d3e9b15c28`)

- `vpn_users` += `telegram_id` (BIGINT NULL, UNIQUE `uq_vpn_users_telegram_id`) и `telegram_username`
  (VARCHAR(64) NULL).
- Таблицы `bot_keys`, `bot_invites` (CHECK `ck_bot_invites_days`: 1..3650), `bot_registrations` (UNIQUE
  `invite_id` и `access_grant_id`, FK именованные). `status` — VARCHAR(16), не Postgres-enum.
- Частичный уникальный индекс `uq_bot_registrations_open_telegram_id` по `telegram_id` при `status IN
  ('PAYMENT_PENDING', 'PROVISIONING')` — имена членов enum, не значения.
- Downgrade удаляет индекс, три таблицы и две колонки с UNIQUE. **Связка пользователей с Telegram и все данные
  бота (ключи, приглашения, регистрации) теряются**; пользователи, подписки и гранты остаются.

## Частичные уникальные индексы

Бизнес-правила «одна живая запись» держит БД (`postgresql_where=...`): одна незавершённая подписка на
пользователя, один незавершённый грант на (подписка, протокол), один живой credential на грант. Нарушение →
`IntegrityError` → 409 (обработчик в `main.py`).

## CONCURRENTLY

- `CREATE/DROP INDEX CONCURRENTLY` — только в `op.get_context().autocommit_block()` (см. `7c1e4a9d2b30`).
- Оборванный CONCURRENTLY оставляет INVALID-индекс, который `IF NOT EXISTS` примет за готовый: перед
  созданием — `drop_index(..., if_exists=True)`.
