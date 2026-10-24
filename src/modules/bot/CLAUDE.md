# modules/bot — Telegram-бот: ключи, приглашения, регистрации

Бот — внешний клиент; в репозитории только сторона панели. Бот видит только Bot API и входит ключом `X-Bot-Key`.
Админ выпускает ключи и приглашения через админский API (JWT). Регистрация по приглашению создаёт пользователя,
подписку, грант и задачу `CREATE_USER` существующей цепочкой (`modules/access`, `modules/jobs`). Веб-интерфейса
пока нет: в панели видны только тексты событий аудита.

Подробно: `docs/bot/api.md` (поля, коды, лимиты, компромиссы), `docs/bot/registration.md` (шаги регистрации,
статусы, причины 409, хук, повтор, сверка), `docs/bot/tests.md` (перечень тестов).

## Файлы

| Путь | Что |
|---|---|
| `models/key.py`, `invite.py`, `registration.py` | `BotKey` (`bot_keys`), `BotInvite` (`bot_invites`), `BotRegistration` (`bot_registrations`), `RegistrationStatus`, `OPEN_STATUSES` |
| `repository.py` | Репозитории трёх моделей; `get_by_hash(lock=True)`, `list_provisioning` (`SKIP LOCKED`) |
| `keys.py` | `BotKeyService`: выпуск (`twb_` + 43 символа), список, отзыв, `heartbeat` |
| `invites.py` | `BotInviteService`: создание (код 24 символа), список, отзыв, `check`, `link`; `invite_status` |
| `registrations.py` | `BotRegistrationService`: `register`, `get`, `by_telegram`, `retry`, `on_issue_finished` (хук воркера), `reconcile` (правило `stale_jobs`) |
| `payment.py` | `PaymentPolicy` и `InviteFree` (всегда `PAID`) |
| `dependencies.py` | `require_bot_key`, `BotContext`, метка `bot_key_required` (её читает тест RBAC) |
| `rate_limits.py` | Лимиты `BOT_*` |
| `schemas.py` | Схемы; имя и ник без управляющих и невидимых символов; `telegram_id` — BIGINT |
| `api/v1/bot.py` | Bot API, `/api/v1/bot` |
| `api/v1/admin.py` | Админский API, `/api/v1/bot-admin` |

## Маршруты

**Bot API** (ключ в `X-Bot-Key`): `POST /heartbeat`, `POST /invites/check`, `POST /registrations` (201),
`GET /registrations/{registration_id}`, `GET /users/by-telegram/{telegram_id}`.

**Админский API** (JWT):

| Право | Маршруты |
|---|---|
| `bot:read` (viewer и выше) | `GET /keys`, `GET /invites`, `GET /registrations[?status=]`, `GET /registrations/{id}` |
| `bot:manage` (operator и выше) | `POST /invites`, `POST /invites/{id}/revoke`, `POST /registrations/{id}/retry` (202) |
| `bot:keys` (только superadmin) | `POST /keys`, `POST /keys/{id}/revoke` |

`POST /keys` и `POST /invites` отдают открытый `key` / `code` (и `link` для приглашения) один раз.

## Авторизация Bot API

- Каждый маршрут Bot API — ровно одна зависимость `require_bot_key`. Маршрут без неё тест RBAC не пропустит.
- В БД — только SHA-256 ключа (`key_hash`); в списке — `key_prefix`. Отозванный ключ (`revoked_at`) перестаёт
  работать сразу.
- Нет ключа, неверный или отозванный → **401 `Invalid bot key`**, одинаково. Ключ не открывает админский API, JWT не
  открывает Bot API.
- Лимиты (`BOT_*`): неудачи по IP, все запросы по ключу, регистрации по ключу. Превышение → 429 с `Retry-After`.
  Значения и порядок проверок — `docs/bot/api.md`.
- Логи: `core/log.py` маскирует `twb_…`, `X-Bot-Key` и поле `code`.

## Регистрация (`registrations.py`)

- Одна транзакция, SSH не делается. Приглашение `FOR UPDATE` → проверки → регистрация `payment_pending` →
  приглашение `used_at` → пользователь, подписка на `days` дней и грант на протокол приглашения (`stage_create`
  сервисов `modules/vpn_users` и `modules/access`) → `provisioning` → `schedule_issue` → один commit.
- Повтор тем же `telegram_id` по своему приглашению возвращает ту же регистрацию и ничего не создаёт.
- Отказ — 409 с `reason`: `invite_invalid`, `invite_expired`, `invite_used`, `telegram_taken`, `email_taken`,
  `conflict` (второй `IntegrityError` подряд). Отказ ничего не создаёт.
- Почта проверяется без учёта регистра (`lower(email)`) только на этом пути.
- Статусы: `payment_pending` → `provisioning` → `active` | `failed`; `failed` → `provisioning` (retry админом).
  Приглашение остаётся занятым и при `failed`.
- Итог выдачи меняется только в двух местах: хук `on_issue_finished` (воркер, в одной транзакции со статусом задачи
  `CREATE_USER` с грантом регистрации) и `reconcile` (правило `stale_jobs`). Кроме `retry` админом.
- Задача — существующая `CREATE_USER`; нового `JobType` нет.
- `PaymentPolicy` подменяется через `BotRegistrationService(payment=...)`. Сейчас `InviteFree` пропускает всех.

## Аудит

`bot_key_created/revoked`, `bot_invite_created/revoked`, `bot_registration_created/activated/failed/retried`;
сущности `bot_key`, `bot_invite`, `bot_registration`. Актор бота — без админа, только IP. В `meta` нет кодов,
ключей, почты и `telegram_id`. Пользователь, созданный ботом, — `vpn_user_created` с `{source: bot}`.

## Принятые компромиссы

- `telegram_id` в пути `GET /users/by-telegram/{id}` попадает в логи запросов.
- `email_taken` и `telegram_taken` — оракул по базе, ограничен 10 регистрациями в минуту на ключ.
- Лимит неудач по IP блокирует и валидный ключ с того же IP; повтор регистрации тоже тратит лимит.
- Любой действующий ключ читает регистрацию по id и статус по любому `telegram_id`.

## Ограничения текущей версии

- `config_url` в ответах всегда `null`; `heartbeat` отвечает `{}`.
- Настроек бота по умолчанию нет: протокол и срок в приглашении обязательны.
- Оплаты нет (`InviteFree`). Уведомлений админу, блокировки пользователя ботом и веб-интерфейса нет.
- Функционального индекса по `lower(email)` нет.
