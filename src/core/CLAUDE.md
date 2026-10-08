# core — общее, не привязанное к домену

Импортируется всеми слоями; сам не импортирует ни `modules/`, ни `infrastructure/`.

| Файл | Что внутри |
|---|---|
| `config.py` | `Settings` (pydantic-settings) из ENV/`.env`; единственный экземпляр `settings`. |
| `database/base.py` | `Base` (DeclarativeBase), миксины `UuidPkMixin`, `TimestampMixin` (tz-aware). |
| `database/postgres.py` | `AsyncEngine`, `async_sessionmaker`, `get_session` (Depends). |
| `redis.py` | `get_redis()` — один клиент `redis.asyncio` на процесс, `decode_responses=True`. |
| `security.py` | Argon2id (`hash_password`/`verify_password`, `DUMMY_PASSWORD_HASH` против тайминг-атаки), JWT access (`create/decode_access_token`), refresh (`generate_refresh_token`, `hash_refresh_token`), Fernet (`encrypt_secret`/`decrypt_secret`). |
| `exceptions.py` | Доменные ошибки, база `AppError`. Коды HTTP для них — в `main.py` (`_STATUS_BY_ERROR`). |
| `ssh_exception.py` | Ошибки выполнения на сервере: `SSHServerError` → `InstallError`, `CommandExecutionError`, `InvalidServerConfigurationError`, `DockerError`. |
| `schemas.py` | `PatchModel` — база PATCH-схем: явный `null` в поле без `NULLABLE` → 422. |
| `log.py` | Логи: `JsonFormatter`/`TextFormatter`, `bind(**поля)` на contextvars (request_id, job_id, server_id), `redact()` — страховка от секретов. |
| `middleware.py` | Чистый ASGI `RequestContextMiddleware`: `X-Request-ID` (берёт из nginx, если `[A-Za-z0-9-]{8,64}`). |
| `rate_limit.py` | `Limit`, `RateLimiter` на Redis (fail-open при сбое Redis), `RateLimitExceeded` → 429 + `Retry-After`. |
| `utils.py` | Сроки жизни токенов. |

## Ошибки → HTTP (main.py)

`NotFoundError` 404 · `InvalidDataError` 422 · `AlreadyExistsError`, `CredentialNotFound`, `LiveCredentialExistsError`,
`InactiveAccessError`, `AdminRuleError`, `SSHHostKeyNotPinnedError` → 409 · `IntegrityError` (сработал уникальный
индекс) → 409 без текста БД. Новый класс ошибки, который должен дойти до клиента осмысленным кодом, — добавить в
`_STATUS_BY_ERROR`; наследник уже зарегистрированного класса получает его код, прочие `AppError` дадут 500.

## Настройки — что важно

- Обязательные без дефолта: `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY` (валидатор: ≥32 символа, не `change_me`),
  `ENCRYPTION_KEY` (Fernet), `JWT_ACCESS_EXPIRE`, `JWT_REFRESH_EXPIRE`, `SSH_CONNECT_TIMEOUT`, `SSH_COMMAND_TIMEOUT`.
- `COOKIE_SECURE` (по умолчанию true; локально и в тестах false), `DOCS_ENABLED` (в проде false), `LOG_FORMAT`.
- Группы: лимиты входа (`LOGIN_*`, `REFRESH_*`), планировщик (`SCHEDULER_*`, `*_INTERVAL_SECONDS`), хранение
  (`JOB_/AUDIT_RETENTION_DAYS`), мониторинг (`METRICS_*`).
- Новую настройку дописывать в `.env.example` и `.env.prod.example` с комментарием.

## Правила

- Секреты в `Settings` — `SecretStr`; доставать `.get_secret_value()` только в месте использования.
- Логировать через `logging.getLogger(__name__)` с `extra={...}`; секреты и stdout SSH в логи не писать.
- `.env` / `.env.prod` не читать и не выводить — там реальные ключи.
