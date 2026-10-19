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

## Импорт клиентов (`f6b2d8a41c73`)

- `jobtype` += `IMPORT_CLIENTS` — в `autocommit_block`, с `IF NOT EXISTS`.
- `vpn_users.email` → NULL. `vpn_credentials`: `encrypted_config` → NULL; `source` (VARCHAR(16), не
  Postgres-enum; `server_default 'ISSUED'` — старые строки становятся ISSUED), `encrypted_psk`,
  `external_name`; CHECK `ck_vpn_credentials_issued_has_config`. `jobs.params` (JSONB, NULL).
- Downgrade: удаляет гранты, у которых остались только `IMPORTED` credential'ы (вместе с ними), затем сами
  `IMPORTED` credential'ы и задачи `IMPORT_CLIENTS`; пользователям без email ставит `<id>@imported.invalid`;
  дальше возвращает NOT NULL и пересоздаёт `jobtype` без нового значения. Гранты с обычным credential'ом,
  пользователи, подписки и peer'ы на сервере не трогаются.

## Частичные уникальные индексы

Бизнес-правила «одна живая запись» держит БД (`postgresql_where=...`): одна незавершённая подписка на
пользователя, один незавершённый грант на (подписка, протокол), один живой credential на грант. Нарушение →
`IntegrityError` → 409 (обработчик в `main.py`).

## CONCURRENTLY

- `CREATE/DROP INDEX CONCURRENTLY` — только в `op.get_context().autocommit_block()` (см. `7c1e4a9d2b30`).
- Оборванный CONCURRENTLY оставляет INVALID-индекс, который `IF NOT EXISTS` примет за готовый: перед
  созданием — `drop_index(..., if_exists=True)`.
