# Справочник (вынесено из CLAUDE.md)

Описательные части: карта файлов, модели, API, операции. Инварианты и грабли остались в CLAUDE.md.

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

## Консольные скрипты (`src/scripts`)

- `bot_key.py`: `create --name N [--raw]`, `revoke` (ключ из stdin), `list`, `seen --within S` (ключ из stdin, код выхода
  0, если бот выходил на связь не позже S секунд назад). Выпуск — `BotKeyService.create(source="cli")`.
- `bot_invite.py`: `protocols`, `create --protocol ID --days N --expires-in 7d|12h [--note] [--raw]`. Выпуск —
  `BotInviteService.create(source="cli")`.
- Ключ и код показываются только в терминале; без терминала скрипт отказывается до выпуска, пока не задан `--raw`.
  Установщики вызывают `create --raw` и передают ключ в `revoke` и `seen` через stdin, не аргументом.

## Аудит

`bot_key_created/revoked`, `bot_invite_created/revoked`, `bot_registration_created/activated/failed/retried`,
`config_link_created`; сущности `bot_key`, `bot_invite`, `bot_registration`, `config_link`. Актор бота — без админа,
только IP. В `meta` нет кодов, ключей, почты и `telegram_id`; у `config_link_created` — `{credential_id, user_id,
source: bot, replaced}`, без токена и хеша. Пользователь, созданный ботом, — `vpn_user_created` с `{source: bot}`. Ключ и приглашение,
выпущенные консолью, — с `source: cli` в `meta`; из админского API этого поля нет.

## Ограничения текущей версии

- `heartbeat` отвечает `{}`.
- Настроек бота по умолчанию нет: протокол и срок в приглашении обязательны.
- Оплаты нет (`InviteFree`). Уведомлений админу, блокировки пользователя ботом и веб-интерфейса нет.
- Функционального индекса по `lower(email)` нет.
