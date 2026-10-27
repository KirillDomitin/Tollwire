# modules/audit — журнал действий

- Модель `audit_logs`: `action` (`AuditAction`), `entity_type` (строка из `AuditEntity`), `entity_id`, `admin_id`
  (NULL — система), `ip_address`, `meta` (JSONB, колонка и поле API называются `metadata`), `created_at`.
- `action` хранится как VARCHAR (`Enum(native_enum=False)`) — новое событие добавляется в enum без миграции.
- События: серверы (`server_*`), учётки (`server_credentials_*`), ключ хоста (`server_host_key_*`), ключ панели
  (`server_ssh_key_install_*`), протоколы (`protocol_*`), выходы шлюза (`gateway_exit_*`), failover шлюза (`gateway_client_moved`, `gateway_failover`, `gateway_exit_recovered`, `gateway_all_exits_down`; без ключей и адресов), «Россия напрямую» (`geoip_list_updated/activated`, `geoip_override_added/removed`, `gateway_ru_applied/apply_failed`; сущность `geoip`), доступы (`credential_issue_*/credential_revoke_*`). Telegram-бот (`bot_key_created/revoked`, `bot_invite_created/revoked`, `bot_registration_created/activated/failed/retried`; сущности `bot_key`, `bot_invite`, `bot_registration`; в `meta` нет кодов, ключей, почты и `telegram_id`; `source: cli` — выпущено консольным скриптом).
- Кто действует — `AuditActor(admin_id, ip)`, передаётся в конструктор сервисов через `Depends(get_audit_actor)`;
  для входа — `get_anonymous_actor`; планировщик — `SYSTEM_ACTOR`; воркер — от имени инициатора задачи
  (`jobs.admin_id`).
- `AuditService.record(...)` не коммитит: запись попадает в ту же транзакцию, что и само действие (откат действия
  откатывает и запись).
- `diff_changes(obj, new_values)` — что поменялось в PATCH (для `*_updated`).
- **Без секретов:** пароли, ключи, токены, конфиги в `meta` не кладутся.
- API: `GET /api/v1/audit-logs`, право `audit:read`. Фильтры: `admin_id`, `action` (несколько значений),
  `entity_type`, `entity_id`, `since`, `until` (граница не включается). Курсор: `before_created_at` и
  `before_id` только вместе, иначе 422. `limit` ≤ 100, `offset` остаётся. Ответ — список с `admin_email`
  (NULL у анонимных событий и у удалённых админов).
- `GET /api/v1/audit-logs/actors` — админы, которые есть в журнале (фильтр «Админ»), право `audit:read`.
- Сортировка `created_at DESC, id DESC`. Индекс `ix_audit_logs_created_at_id`; одиночного индекса по
  `created_at` больше нет.
- Email админов видят все роли с `audit:read` — осознанное решение.
- Хранение — правило `retention` (`AUDIT_RETENTION_DAYS`, по умолчанию 365).
- Страница `/audit` — во фронтенде, см. `frontend/CLAUDE.md`.
