# modules/audit — журнал действий

- Модель `audit_logs`: `action` (`AuditAction`), `entity_type` (строка из `AuditEntity`), `entity_id`, `admin_id`
  (NULL — система), `ip_address`, `meta` (JSONB, колонка и поле API называются `metadata`), `created_at`.
- `action` хранится как VARCHAR (`Enum(native_enum=False)`) — новое событие добавляется в enum без миграции.
- Кто действует — `AuditActor(admin_id, ip)`, передаётся в конструктор сервисов через `Depends(get_audit_actor)`;
  для входа — `get_anonymous_actor`; планировщик — `SYSTEM_ACTOR`; воркер — от имени инициатора задачи
  (`jobs.admin_id`).
- `AuditService.record(...)` не коммитит: запись попадает в ту же транзакцию, что и само действие (откат действия
  откатывает и запись).
- `diff_changes(obj, new_values)` — что поменялось в PATCH (для `*_updated`).
- **Без секретов:** пароли, ключи, токены, конфиги в `meta` не кладутся.
- API: `GET /api/v1/audit-logs` (фильтры `AuditLogFilter`, limit/offset), право `audit:read`.
- Хранение — правило `retention` (`AUDIT_RETENTION_DAYS`, по умолчанию 365).
- Фронтенд журнала — этап 20.4 (не сделан); словарь событий для интерфейса — `frontend/src/lib/audit.ts`
  (не закоммичен).
