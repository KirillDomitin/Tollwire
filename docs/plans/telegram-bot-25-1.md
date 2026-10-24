# План: этап 25.1 — основа бота в панели

ТЗ: [telegram-bot.md](telegram-bot.md) (раздел 12, этап 25.1). Без UI.

## Задача

Модуль `src/modules/bot`: ключи бота, приглашения, регистрации с шагом оплаты (`PaymentPolicy`, сейчас
`InviteFree`), Bot API (`/api/v1/bot/*`, `X-Bot-Key`), админский API (`/api/v1/bot-admin/*`), права `bot:read` /
`bot:manage` / `bot:keys`, аудит. Доступ выдаётся существующей цепочкой пользователь → подписка → грант →
задача `CREATE_USER`.

## Принятые решения

- **Р1-A.** Выдача — существующая `CREATE_USER`; пользователь, подписка и грант создаются в запросе
  `POST /registrations` (только БД). Нового `JobType` нет.
- **Р2-A.** Из `VPNUserService.create`, `SubscriptionService.create`, `AccessGrantService.create` выделяются
  `stage_*` (проверки, flush, аудит, без commit); публичные методы = `stage_*` + commit.
- **Р3-A.** Хук `BotRegistrationService.on_issue_finished` в воркере (в той же транзакции, что и финальный статус
  Job) + `reconcile()` в правиле `stale_jobs` планировщика.
- Приглашение занимается при `POST /registrations` (FOR UPDATE + UNIQUE(`invite_id`)), а не на `active`;
  при `failed` остаётся занятым, админ повторяет регистрацию.
- `bot_settings` — не в 25.1: `protocol_id` и `days` при создании приглашения обязательны.
- `heartbeat` отвечает `{}` (поля — в 25.5). `config_url: str | None` в ответах всегда `null` до 25.3.
- Email на пути бота сравнивается без учёта регистра (`lower(email)`); функциональный индекс — долг в GitHub Issues.
- Границы: 25.1 — `heartbeat`, `invites/check`, `POST/GET /registrations`, `GET /users/by-telegram/{id}`.
  `config-link` — 25.3, `outbox/*` — 25.4, `admin-links` — 25.5.

## Таблицы 25.1

- `vpn_users`: + `telegram_id` BIGINT NULL (`uq_vpn_users_telegram_id`), + `telegram_username` VARCHAR(64) NULL.
- `bot_keys`: `id`, `name`, `key_hash` (UNIQUE), `key_prefix` (8 символов), `created_by` (FK admins SET NULL),
  `created_at`, `last_seen_at`, `last_seen_version`, `bot_username`, `revoked_at`.
- `bot_invites`: `id`, `code_hash` (UNIQUE), `protocol_id` (FK protocols SET NULL), `days` (CHECK 1..3650),
  `expires_at` NOT NULL, `note`, `created_by` (FK SET NULL), `created_at`, `used_at`, `revoked_at`.
- `bot_registrations`: `id`, `invite_id` (FK, UNIQUE), `bot_key_id` (FK SET NULL), `telegram_id`,
  `telegram_username`, `name`, `email`, `status` VARCHAR(16), `user_id` (FK vpn_users SET NULL),
  `access_grant_id` (FK SET NULL, UNIQUE), `error`, `created_at`, `updated_at`, `finished_at`; частичный
  уникальный индекс по `telegram_id` при `status IN ('payment_pending','provisioning')`.
- Статусы: `payment_pending` → `provisioning` → `active` | `failed`; `failed` → `provisioning` (retry).

## Шаги

- [ ] 1. `core`: `security.py` — общие `generate_token` / `hash_token`; `config.py` — лимиты `BOT_*` (+ `.env.example`,
  `.env.prod.example`); `log.py` — префикс ключа и поля `x-bot-key`, `code` в `_SECRET_PATTERNS`; `exceptions.py` —
  `BotAuthError` (401), `RegistrationConflictError(reason)` (409) + коды в `main.py`.
- [ ] 2. Модели и миграция: telegram-поля `vpn_users`; `modules/bot/models/{key,invite,registration}.py`; импорты в
  `alembic/env.py`; одна ревизия (именованные ограничения, CHECK), downgrade удаляет три таблицы и две колонки.
- [ ] 3. `vpn_users` / `access`: `stage_*` (Р2-A); `VPNUserRepository.get_by_telegram_id`; последняя подписка
  пользователя в `SubscriptionRepository`.
- [ ] 4. `modules/bot`: репозитории, `payment.py` (`PaymentPolicy`, `InviteFree`), сервисы ключей, приглашений,
  регистраций (`register`, `get`, `by_telegram`, `retry`, `on_issue_finished`, `reconcile`); `dependencies.py`
  (`require_bot_key`, метка `bot_key_required`), `rate_limits.py`.
- [ ] 5. Роутеры `api/v1/bot.py`, `api/v1/admin.py`; `include_router` в `main.py`.
- [ ] 6. Воркер и планировщик: хук `on_issue_finished` в обеих ветках `worker.py`; `reconcile` в `stale_jobs`.
- [ ] 7. Права: три `Permission` в `permissions.py` (`BOT_KEYS` — только superadmin).
- [ ] 8. Аудит: сущности `bot_key`, `bot_invite`, `bot_registration`; события `bot_key_created/revoked`,
  `bot_invite_created/revoked`, `bot_registration_created/activated/failed/retried`; без кодов, ключей, почты,
  `telegram_id`.
- [ ] 9. Фронтенд: только `npm run gen:api`.
- [ ] 10. Документация: `modules/bot/CLAUDE.md` (новый), `vpn_users`, `access`, `auth`, `audit`,
  `infrastructure/workers`, `alembic`, `core`, `tests`, корневой «Где что».

## Тесты

- `unit/`: схемы (управляющие символы, длины, почта), алфавит и длина кода ≤ 64, хеш, `InviteFree`, статус приглашения.
- `services/test_bot_registration.py`: успешный путь; повтор тем же `telegram_id` → та же регистрация; чужой
  `telegram_id` → `invite_used`; `email_taken` / `telegram_taken` без побочных эффектов; истёкшее / отозванное /
  неизвестное приглашение, удалённый протокол; заглушка `PENDING`; гонки (приглашение, почта); хук (успех, сбой
  SSH с `redact`); `reconcile`; retry (успех, грант отозван → 409, повторный → 409).
- `integration/test_bot_flow.py`: ключ → приглашение → `POST /registrations` → `drain(worker)` на `FakeServer` →
  `active` → `users/by-telegram`; сбой → retry → `active`; шлюз; в аудите нет кода, ключа и почты.
- Авторизация бота: 401 на каждом маршруте без / с неверным / отозванным ключом; JWT админа на Bot API → 401, ключ
  бота на админском API → 401; 429 с `Retry-After`; heartbeat обновляет `last_seen_at`, `bot_username`, версию.
- `test_rbac.py`: `/api/v1/bot/` исключить из `API_ROUTES`; метка `bot_key_required` на каждом ботовом маршруте;
  матрица прав; `POST /api/v1/bot-admin/keys` → `BOT_KEYS` в `CRITICAL`.
- `services/test_migrations.py`: upgrade → downgrade → upgrade новой ревизии.

## Риски

- Живой прогон выдаёт клиента на реальном сервере — только с согласия пользователя; помнить про dev/prod-панель на
  одном сервере (правило `sync` удаляет клиентов другой панели).
- Шлюз без исправных выходов: регистрация `active`, но интернета нет — как при выдаче из панели.
- Почты с разным регистром обходят уникальность `vpn_users.email` — проверка `lower(email)` на пути бота, индекс — долг.
- Секреты: ключ и код не в логах, аудите и `Job.params`; текст `IntegrityError` наружу не отдаётся.
- Downgrade миграции теряет связку с Telegram и данные бота — записать в `alembic/CLAUDE.md`.
- Хук воркера упал в ветке сбоя → задача станет stale, `reconcile` переведёт регистрацию в `failed`.
