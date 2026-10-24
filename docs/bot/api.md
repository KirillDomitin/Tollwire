# modules/bot — Bot API и админский API: маршруты, коды, лимиты

Инварианты — в `src/modules/bot/CLAUDE.md`. Здесь — поля, коды ответов и параметры лимитов.

## Bot API (`/api/v1/bot`, ключ в `X-Bot-Key`)

У каждого маршрута ровно одна зависимость `require_bot_key`; тест RBAC проверяет это по метке `bot_key_required`.

| Метод и путь | Запрос | Ответ | Коды |
|---|---|---|---|
| `POST /heartbeat` | `bot_username` (`^@?[A-Za-z0-9_]{1,32}$`), `version` (1–32 символа) | `{}` | 200; 422 |
| `POST /invites/check` | `code` (1–64) | `{status}`: `valid`, `used`, `expired`, `revoked`, `unknown` | 200; 422 |
| `POST /registrations` | `code`, `telegram_id` (1…2⁶³−1), `telegram_username` (необяз., ≤64), `name` (1–64 после обрезки пробелов), `email` | `{registration_id, status, config_url: null}` | 201; 409 с `reason`; 422; 429 |
| `GET /registrations/{registration_id}` | — | `{registration_id, status, config_url: null}` | 200; 404; 422 (не UUID) |
| `GET /users/by-telegram/{telegram_id}` | — | `{name, valid_until, protocol, config_url: null}` | 200; 404 (`User not found`); 422 |

- `status` регистрации: `payment_pending`, `provisioning`, `active`, `failed`.
- `/users/by-telegram`: `valid_until` — конец последней подписки пользователя (по дате создания, в любом статусе);
  `protocol` — тип протокола гранта этой подписки, `null` без грантов или подписки.
- `/invites/check` не раскрывает протокол, срок и получателя приглашения.
- `config_url` всегда `null`: страницы конфига пока нет.
- Имена (`name`, `telegram_username`) не могут содержать управляющие, невидимые, суррогатные символы и разделители
  строк (категории Unicode Cc, Cf, Cs, Zl, Zp); иначе 422.
- Код приглашения — параметр `start` ссылки `t.me/<бот>?start=<код>`, до 64 символов.

## Админский API (`/api/v1/bot-admin`, JWT)

Все маршруты — за `get_current_admin` и `require(Permission.X)`.

| Метод и путь | Право | Что |
|---|---|---|
| `GET /keys` | `bot:read` | ключи без секрета: `id`, `name`, `key_prefix`, `created_at`, `last_seen_at`, `last_seen_version`, `bot_username`, `revoked_at` |
| `POST /keys` | `bot:keys` | `{name}` → 201; поле `key` (открытое значение) — только в этом ответе |
| `POST /keys/{key_id}/revoke` | `bot:keys` | 200; 404 |
| `GET /invites` | `bot:read` | приглашения без кода, поле `status`: `valid`, `used`, `expired`, `revoked` |
| `POST /invites` | `bot:manage` | `{protocol_id, days (1–3650), expires_at (с часовым поясом, в будущем), note (необяз., ≤100)}` → 201; `code` и `link` — только в этом ответе |
| `POST /invites/{invite_id}/revoke` | `bot:manage` | 200 (повтор — без изменений); 404 |
| `GET /registrations[?status=]` | `bot:read` | регистрации, новые первыми; `telegram_id`, `email` видны админу |
| `GET /registrations/{registration_id}` | `bot:read` | 404 |
| `POST /registrations/{registration_id}/retry` | `bot:manage` | 202; только из `failed`; 404; 409 (см. `registration.md`) |

- `link` в ответе на создание приглашения — `https://t.me/<имя бота>?start=<код>`, если имя бота известно: его
  сообщает последний heartbeat действующего ключа. Иначе `null`.
- `POST /invites` с неизвестным `protocol_id` → 404; `expires_at` в прошлом → 422.
- Список ключей и приглашений открытых значений не содержит.

## Авторизация Bot API (`dependencies.py`, `rate_limits.py`)

Порядок проверок в `require_bot_key`:

1. Лимит неудач по IP (`BOT_AUTH_IP_*`): превышен → 429, ключ в БД не ищется.
2. Значение `X-Bot-Key` → SHA-256 → поиск среди неотозванных ключей. Заголовка нет, ключ не найден или отозван →
   неудача, запись в лимит по IP, 401 `Invalid bot key`. Причина для ответа не различается.
3. Лимит запросов по ключу (`BOT_KEY_*`): превышен → 429.
4. Для `POST /registrations` — лимит регистраций по ключу (`BOT_REGISTRATIONS_*`), до обращения к сервису.

| Лимит | Переменные (`core/config.py`, `.env*.example`) | По умолчанию | Что считается |
|---|---|---|---|
| Неудачи авторизации по IP | `BOT_AUTH_IP_MAX_FAILURES`, `BOT_AUTH_IP_WINDOW_SECONDS` | 30 за 60 с | нет, неверный или отозванный ключ |
| Все запросы по ключу | `BOT_KEY_MAX_REQUESTS`, `BOT_KEY_WINDOW_SECONDS` | 300 за 60 с | каждый запрос, прошедший авторизацию |
| Регистрации по ключу | `BOT_REGISTRATIONS_MAX`, `BOT_REGISTRATIONS_WINDOW_SECONDS` | 10 за 60 с | каждый `POST /registrations`, включая повтор |

- 429 отдаётся с `Retry-After`. При недоступном Redis лимиты не действуют (fail-open, см. `src/core/CLAUDE.md`).
- Ключ: префикс `twb_` и 43 символа (`generate_token(32)`). В БД — только SHA-256 (`key_hash`, UNIQUE) и первые 8
  символов (`key_prefix`). Открытое значение показывается один раз.
- Код приглашения: 24 символа (`generate_token(18)`), в БД — SHA-256 (`code_hash`, UNIQUE).
- Аудит и логи: актор бота без админа (только IP). `core/log.py` маскирует `twb_…`, значение `X-Bot-Key` и поле
  `code` (от 8 символов).
- Ключ, отозванный в панели, перестаёт работать на следующем запросе.

## Принятые компромиссы

- `telegram_id` в пути `GET /users/by-telegram/{telegram_id}` попадает в логи запросов (access-лог сервера).
- `email_taken` и `telegram_taken` — оракул по базе: по ответу видно, занят ли email или Telegram. Ограничено 10
  регистрациями в минуту на ключ.
- Лимит неудач по IP блокирует и валидный ключ с того же IP (проверка до поиска ключа).
- Повтор регистрации тем же `telegram_id` тоже тратит лимит регистраций.
- Ключ не привязан к «своим» регистрациям: любой действующий ключ читает регистрацию по `registration_id` и статус
  по любому `telegram_id`.
- Почта сравнивается без учёта регистра только на пути бота (`lower(email)`); функционального индекса нет.
