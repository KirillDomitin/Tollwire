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

## Частичные уникальные индексы

Бизнес-правила «одна живая запись» держит БД (`postgresql_where=...`): одна незавершённая подписка на
пользователя, один незавершённый грант на (подписка, протокол), один живой credential на грант. Нарушение →
`IntegrityError` → 409 (обработчик в `main.py`).

## CONCURRENTLY

- `CREATE/DROP INDEX CONCURRENTLY` — только в `op.get_context().autocommit_block()` (см. `7c1e4a9d2b30`).
- Оборванный CONCURRENTLY оставляет INVALID-индекс, который `IF NOT EXISTS` примет за готовый: перед
  созданием — `drop_index(..., if_exists=True)`.
