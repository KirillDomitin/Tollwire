# modules/bot — Telegram-бот: ключи, приглашения, регистрации

Бот — отдельный пакет `bot/` (см. `bot/CLAUDE.md`); здесь только сторона панели. Бот видит только Bot API и входит
ключом `X-Bot-Key`.
Админ выпускает ключи и приглашения через админский API (JWT). Регистрация по приглашению создаёт пользователя,
подписку, грант и задачу `CREATE_USER` существующей цепочкой (`modules/access`, `modules/jobs`). Веб-интерфейса
пока нет: в панели видны только тексты событий аудита.

Страница конфига: ссылку на конфиг выдаёт `POST .../config-link` (только бот); открывает её публичный
`POST /api/v1/public/config` без входа и без ключа.

Подробно: `docs/bot/api.md` (поля, коды, лимиты, компромиссы), `docs/bot/registration.md` (шаги регистрации,
статусы, причины 409, хук, повтор, сверка), `docs/bot/config-page.md` (ссылка, жизненный цикл, проверки открытия,
безопасность), `docs/bot/tests.md` (перечень тестов, в том числе бота и установки).

## Файлы

| Путь | Что |
|---|---|
| `models/key.py`, `invite.py`, `registration.py` | `BotKey` (`bot_keys`), `BotInvite` (`bot_invites`), `BotRegistration` (`bot_registrations`), `RegistrationStatus`, `OPEN_STATUSES` |
| `models/config_link.py` | `ConfigLink` (`config_links`): хеш токена, клиент (CASCADE), `revoked_at`, `last_opened_at`; частичный уникальный индекс «одна живая ссылка на клиента» |
| `repository.py` | Репозитории трёх моделей; у приглашений `get_by_hash(lock=True)`, у ключей `get_by_hash` (в том числе отозванный, для `revoke` из консоли), `get_active_by_hash` (для `seen`), `list_provisioning` (`SKIP LOCKED`). `ConfigLinkRepository`: клиенты человека, блокировка клиента, открытие по хешу токена |
| `config_links.py` | `ConfigLinkService`: `issue_for_telegram` (выпуск), `open` (открытие по токену), `url_of` (ответ бота); коды причин `CONFIG_UNAVAILABLE`, `NOT_READY`, `NO_ACCESS`, `NOT_CONFIGURED`; `file_name_of` |
| `keys.py` | `BotKeyService`: выпуск (`twb_` + 43 символа), список, отзыв, `heartbeat`; `create` и `revoke` принимают `source` |
| `invites.py` | `BotInviteService`: создание (код 24 символа, `source`), список, отзыв, `check`, `link`; `invite_status` |
| `registrations.py` | `BotRegistrationService`: `register`, `get`, `by_telegram`, `retry`, `on_issue_finished` (хук воркера), `reconcile` (правило `stale_jobs`) |
| `payment.py` | `PaymentPolicy` и `InviteFree` (всегда `PAID`) |
| `dependencies.py` | `require_bot_key`, `BotContext`, метка `bot_key_required` (её читает тест RBAC) |
| `rate_limits.py` | Лимиты `BOT_*` и `PUBLIC_CONFIG_*` (страница конфига, по IP) |
| `schemas.py` | Схемы; имя и ник без управляющих и невидимых символов; `telegram_id` — BIGINT; `ConfigLinkRead`, `PublicConfigRead`. `config_url` в регистрации и статусе не отдаётся |
| `api/v1/bot.py` | Bot API, `/api/v1/bot` |
| `api/v1/admin.py` | Админский API, `/api/v1/bot-admin` |
| `api/v1/public.py` | Публичный API страницы конфига, `/api/v1/public`: без ключа и входа, `no-store`, `noindex`, лимиты по IP |

## Маршруты

**Bot API** (ключ в `X-Bot-Key`): `POST /heartbeat`, `POST /invites/check`, `POST /registrations` (201),
`GET /registrations/{registration_id}`, `GET /users/by-telegram/{telegram_id}`,
`POST /users/by-telegram/{telegram_id}/config-link` (новая ссылка на страницу конфига; прежняя гаснет).

**Публичный API** (без ключа и входа): `POST /api/v1/public/config` с `{token}` → `PublicConfigRead`. Любой отказ —
404 с одинаковым телом.

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
- Логи: `core/log.py` маскирует `twb_…`, `X-Bot-Key`, поле `code` и поле `token` (страница конфига).

## Консольные скрипты (`src/scripts`)

- `bot_key.py`: `create --name N [--raw]`, `revoke` (ключ из stdin), `list`, `seen --within S` (ключ из stdin, код выхода
  0, если бот выходил на связь не позже S секунд назад). Выпуск — `BotKeyService.create(source="cli")`.
- `bot_invite.py`: `protocols`, `create --protocol ID --days N --expires-in 7d|12h [--note] [--raw]`. Выпуск —
  `BotInviteService.create(source="cli")`.
- Ключ и код показываются только в терминале; без терминала скрипт отказывается до выпуска, пока не задан `--raw`.
  Установщики вызывают `create --raw` и передают ключ в `revoke` и `seen` через stdin, не аргументом.

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

## Страница конфига (`config_links.py`)

- Ссылку выдаёт только `config-link`; регистрация и статус её не отдают. Токен виден один раз, в БД — SHA-256.
- Адрес — `settings.public_base_url`: `PUBLIC_URL`, иначе `PANEL_DOMAIN` и `HTTPS_PORT`. Не задан → 409 `not_configured`
  (проверка первой) и WARNING в лог.
- Клиент — выданный панелью с конфигом и живым доступом; среди них — клиент гранта регистрации через бота, иначе самый
  новый. Только импортированные → `config_unavailable`; живой доступ с незавершённым `CREATE_USER` → `not_ready`;
  иначе → `no_access`. Нет человека → 404.
- Выпуск — одна транзакция: клиент `FOR UPDATE` → отзыв живой ссылки этого клиента → вставка → аудит → commit.
  Два выпуска идут по очереди за блокировкой клиента; частичный индекс — страховка: повтор один раз, второй конфликт →
  409 `no_access`.
- Ссылка действительна, пока одновременно: `revoked_at` пуст, клиент не отозван и `ISSUED`, `inactive_reason` в силе.
  Проверка — при каждом открытии; ни планировщик, ни хуки отзыва ссылку не трогают. `revoked_at` ставится только при
  замене новой ссылкой.
- Открытие (`open`): формат токена → поиск по хешу → `inactive_reason` → `render_config` (единственная расшифровка,
  только после проверок) → `last_opened_at`. В аудит не пишется.
- Лимиты открытия — `PUBLIC_CONFIG_*`: считаются неудачи по IP, не открытия (`rate_limits.py`).
- Подробно — `docs/bot/config-page.md`.

## Аудит

`bot_key_created/revoked`, `bot_invite_created/revoked`, `bot_registration_created/activated/failed/retried`,
`config_link_created`; сущности `bot_key`, `bot_invite`, `bot_registration`, `config_link`. Актор бота — без админа,
только IP. В `meta` нет кодов, ключей, почты и `telegram_id`; у `config_link_created` — `{credential_id, user_id,
source: bot, replaced}`, без токена и хеша. Пользователь, созданный ботом, — `vpn_user_created` с `{source: bot}`. Ключ и приглашение,
выпущенные консолью, — с `source: cli` в `meta`; из админского API этого поля нет.

## Принятые компромиссы

- `telegram_id` в пути `GET /users/by-telegram/{id}` попадает в логи запросов.
- `email_taken` и `telegram_taken` — оракул по базе, ограничен 10 регистрациями в минуту на ключ.
- Лимит неудач по IP блокирует и валидный ключ с того же IP; повтор регистрации тоже тратит лимит.
- Любой действующий ключ читает регистрацию по id и статус по любому `telegram_id`.
- API доверяет `X-Forwarded-For` из сети `edge`, где стоит и бот: скомпрометированный бот подменяет IP в логах и лимитах
  по IP (`bot/CLAUDE.md`).
- Токен во фрагменте адреса страницы конфига остаётся в адресной строке и истории браузера (`docs/bot/config-page.md`).

## Ограничения текущей версии

- `heartbeat` отвечает `{}`.
- Настроек бота по умолчанию нет: протокол и срок в приглашении обязательны.
- Оплаты нет (`InviteFree`). Уведомлений админу, блокировки пользователя ботом и веб-интерфейса нет.
- Функционального индекса по `lower(email)` нет.
