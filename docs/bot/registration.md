# modules/bot — регистрация по приглашению: шаги, статусы, отказы, выдача

Инварианты — в `src/modules/bot/CLAUDE.md`. Коды и поля маршрутов — в `docs/bot/api.md`.

## Шаги `POST /registrations` (`BotRegistrationService.register`)

Всё в одной транзакции. SSH в запросе не выполняется: клиента создаёт воркер по задаче `CREATE_USER`.

1. Приглашение по хешу кода — `SELECT … FOR UPDATE`. Не найдено → `invite_invalid`.
2. Состояние приглашения (`invite_status`):
   - `used`: если регистрация по этому приглашению уже есть и её `telegram_id` совпадает с запросом, возвращается она
     (201, ничего не создаётся). Иначе → `invite_used`.
   - `expired` (`expires_at <= now`) → `invite_expired`.
   - `revoked`, либо протокол приглашения удалён (`protocol_id` NULL) → `invite_invalid`.
3. Есть пользователь с этим `telegram_id` или незавершённая регистрация (`payment_pending`, `provisioning`) с ним →
   `telegram_taken`.
4. Пользователь с такой почтой без учёта регистра → `email_taken`.
5. Создаётся регистрация `payment_pending`; приглашение получает `used_at`; аудит `bot_registration_created`.
6. Политика оплаты (`PaymentPolicy.check`): `PENDING` → commit, регистрация ждёт, доступа нет. `InviteFree` всегда
   `PAID`.
7. Выдача доступа без commit (`_stage_access`):
   - пользователь: `VPNUserService.stage_create` — `username` = имя, `email`, `telegram_id`, `telegram_username`,
     статус `ACTIVE`; аудит `vpn_user_created` с `{source: bot}`;
   - подписка на `days` дней с текущего момента: `SubscriptionService.stage_create`;
   - грант на протокол приглашения: `AccessGrantService.stage_create`;
   - регистрация → `provisioning`, заполняются `user_id` и `access_grant_id`.
   Протокол удалён между шагами (`NotFoundError`) → откат, `invite_invalid`.
8. `VPNCredentialService.schedule_issue(grant_id)` ставит `CREATE_USER`. Один commit фиксирует регистрацию,
   приглашение, пользователя, подписку, грант и задачу.

Ответ: 201, `status: provisioning`.

## Отказы (409, поле `reason`)

Отказ не создаёт ни пользователя, ни подписки, ни гранта. Снимается только блокировка приглашения.

| `reason` | Когда |
|---|---|
| `invite_invalid` | нет такого кода; приглашение отозвано; протокол приглашения удалён |
| `invite_expired` | срок приглашения истёк |
| `invite_used` | приглашение занято другим `telegram_id` или регистрация по нему не совпадает с запросом |
| `telegram_taken` | пользователь с этим `telegram_id` существует или регистрация с ним в работе |
| `email_taken` | почта занята (без учёта регистра) |
| `conflict` | второй `IntegrityError` подряд (гонка, которую не поймали проверки) |

Порядок проверок — как в шагах: `invite_*` раньше `telegram_taken`, `telegram_taken` раньше `email_taken`.

## Гонки и страховка БД

- Приглашение блокируется `FOR UPDATE`: два `telegram_id` на один код — проходит один.
- Уникальности, которые держит БД: `bot_registrations.invite_id` (приглашение одноразовое),
  `bot_registrations.access_grant_id`, частичный индекс по `telegram_id` для `payment_pending`/`provisioning`,
  `vpn_users.telegram_id`, `vpn_users.email`.
- `IntegrityError` → откат и второй проход с чистого листа. Повторный `IntegrityError` → 409 `conflict`.

## Статусы

| Статус | Значение | Переходы |
|---|---|---|
| `payment_pending` | ждёт оплаты; доступа нет | → `provisioning` (политика оплаты пропустила) |
| `provisioning` | пользователь, подписка и грант созданы; `CREATE_USER` в очереди или идёт | → `active` (хук SUCCESS или `reconcile` при живом клиенте); → `failed` (хук FAILED или `reconcile`) |
| `active` | клиент выдан | — |
| `failed` | выдача не удалась; пользователь, подписка и грант остаются | → `provisioning` (`retry` админом) |

- `payment_pending` сейчас не наступает: `InviteFree` пропускает регистрацию сразу.
- Приглашение остаётся `used` и при `failed`, и при `active`.
- Пока пользователь с этим `telegram_id` существует, новая регистрация с ним — `telegram_taken`.

## Итог выдачи: хук воркера (`on_issue_finished`)

Воркер (`infrastructure/workers/worker.py`) вызывает хук для каждой завершённой `CREATE_USER` с `access_grant_id`
до commit, в обеих ветках (SUCCESS и FAILED), в одной транзакции со статусом задачи.

- Регистрации по этому гранту нет или она не в `provisioning` → ничего не делается.
- SUCCESS → `active`, `finished_at`, аудит `bot_registration_activated`.
- FAILED → если есть другая незавершённая `CREATE_USER` на тот же грант (админ уже повторил), ничего не делается.
  Иначе → `failed`, `error` = текст задачи (уже без секретов, до 1000 символов), аудит `bot_registration_failed`
  с `job_id`.
- Упавший хук не даёт зафиксировать итог задачи: задача остаётся `RUNNING`, её закроет правило `stale_jobs`, а
  регистрацию — `reconcile`.

## Сверка: `reconcile()` (правило `stale_jobs`)

Для каждой регистрации в `provisioning` (`FOR UPDATE SKIP LOCKED`):

- нет `access_grant_id` → `failed`;
- есть незавершённая `CREATE_USER` на грант → не трогается;
- есть живой клиент (`VPNCredential` без `revoked_at`) → `active`;
- иначе → `failed`, `error` = текст последней задачи на грант (если есть).

Правило `stale_jobs` записывает число закрытых регистраций в сводку: `closed N bot registrations`. Повтор безопасен:
уже закрытые регистрации не меняются.

## Повтор выдачи админом (`retry`)

- Только из `failed`; иначе 409 `Only a failed registration can be retried`.
- Пользователь или грант удалены (`SET NULL`) → 409: выдать вручную.
- Живой клиент уже есть → регистрация сразу `active` (это не сбой).
- Иначе: статус `provisioning`, `error` и `finished_at` сбрасываются, аудит `bot_registration_retried`, новая задача
  `CREATE_USER` на тот же грант. Пользователь и грант не пересоздаются.
- Доступ отозван или выдача уже идёт → 409, статус остаётся `failed`.

## Граничные случаи

- Протокол удалён после выдачи приглашения: `protocol_id` становится `NULL`, приглашение — `invite_invalid`.
- Пользователя удалили: `user_id` становится `NULL`, регистрация остаётся в истории; повтор — 409.
- Грант удалили: `access_grant_id` становится `NULL`; `reconcile` переводит регистрацию в `failed`.
- Хук видит только гранты регистраций бота: `CREATE_USER` по обычным доступам на него не влияет.
