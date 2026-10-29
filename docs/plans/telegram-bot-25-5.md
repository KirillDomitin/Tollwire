# План: этап 25.5 — уведомления админу

ТЗ: [telegram-bot.md](telegram-bot.md) (разделы 4, 5, 8, 9, 12; этап 25.5). Готовые этапы: [25.1](telegram-bot-25-1.md),
[25.2](telegram-bot-25-2.md), [25.3](telegram-bot-25-3.md), [25.4](telegram-bot-25-4.md).

## Задача

Админ привязывает свой Telegram к панели одноразовым кодом и получает в Telegram сообщения о важных событиях: сервер
недоступен или снова доступен, отказы и восстановление выходов шлюза, сбои задач, неудачная регистрация через бота,
подмена ключа SSH-хоста. Отдельно бот сам замечает, что панель не отвечает дольше порога, и пишет «панель недоступна»,
а потом «восстановлена». Готово, когда при падении сервера приходит ровно одно сообщение, а остановка API даёт
«панель недоступна» и «восстановлена».

Допущения:
- бот один (ТЗ §13);
- у одного админа одна привязка, один Telegram привязан не больше чем к одному админу;
- часового пояса в панели нет: время в сообщениях — по `BOT_NOTIFY_TIMEZONE` (по умолчанию UTC);
- SSH и задачи воркера нужны только `server_health_changed`, и только существующие (`HEALTH_CHECK`).

## Не входит

- Уведомления VPN-пользователям (истекает доступ и т. п.).
- Отслеживание самого бота панелью: если бот молчит, панели некуда писать (есть признак `stale` на странице «Бот»).
- Метрики (CPU, диск) как повод для уведомлений.
- Тихие часы.
- Английские тексты уведомлений.
- Отвязка чужого Telegram суперадмином: при отключении админа уведомления прекращаются и так.

## Решения пользователя

Приняты рекомендованные варианты **Р1–Р11 = A**:

- **Р1.** Источник событий — курсор по `audit_logs` (правило планировщика `bot_notify`) с перекрытием
  `BOT_NOTIFY_OVERLAP_SECONDS` (2100 с, больше `STALE_JOB_TIMEOUT_SECONDS`) и таблицей `bot_notify_seen`. Перекрытие
  нужно потому, что `created_at` — `now()` начала транзакции (`src/modules/audit/models/audit_log.py`), а задача
  воркера коммитит позже (`src/modules/servers/health.py`). Цена: задержка до одного тика (15 с).
- **Р2.** Получатель в очереди — `bot_outbox.user_id` становится NULL, добавляется `admin_link_id` (FK CASCADE), CHECK
  «ровно один получатель, соответствующий `kind`».
- **Р3.** Текст пишет панель (обычный текст в `bot_outbox.text`), бот пересылает как есть. Тексты — в одном модуле
  `src/modules/bot/notify_texts.py`.
- **Р4.** События выбирает каждый админ сам (`admin_telegram_links.events`); значения по умолчанию — из каталога.
  Колонки `notify_events` в `bot_settings` не заводим.
- **Р5.** Новое право `bot:notify` (viewer, operator, superadmin): получить код, отвязать, выбрать события, проба.
- **Р6.** Бот узнаёт чаты и порог «панель недоступна» из ответа `POST /heartbeat`.
- **Р7.** `claim` принимает `kinds` (по умолчанию `["news","config"]`); бот 0.2.0 строки `notify` не видит. В UI —
  предупреждение «бот устарел».
- **Р8.** Уведомлять при первой же смене статуса (проверка раз в 10 мин). Подтверждение двумя проверками — позже,
  если будет шумно.
- **Р9.** `server_health_changed` пишется при любой смене `health_status` с `{from, to, reason}`; уведомляет каталог
  только о `to=down` (из не-down) и `from=down` (в не-down).
- **Р10.** Пробная отправка себе — сейчас: «Проверить уведомления» (plain) и «Отправить новость себе на пробу»
  (`format=html`, отложено из 25.4).
- **Р11.** Привязка — ссылка `t.me/<бот>?start=<код>` (код 32 символа; у приглашения 24, `/start` различает по длине)
  плюс команда `/admin <код>`.

## Каталог событий (`src/modules/bot/notify_catalog.py`)

Ключ → (действия аудита, фильтр по `meta`, группа, включено по умолчанию, функция текста):

- `server_down`, `server_recovered` — по `server_health_changed`, включены;
- `gateway_all_exits_down`, `gateway_failover`, `gateway_exit_recovered` — включены;
- `jobs_failed` — все `*_failed` задач: `server_install`, `server_sync`, `server_discover`, `server_ssh_key_install`,
  `protocol_install/remove`, `credential_issue/revoke`, `clients_import`, `gateway_exit_add/remove`,
  `gateway_ru_apply`; включено;
- `bot_registration_failed` — включено;
- `server_host_key_mismatch` — включено (безопасность);
- `login_locked` — выключено;
- `panel_down` — псевдособытие для бота (Р6), включено.

Ключи каталога — не действия аудита: схема `events` проверяется по каталогу, неизвестный ключ → 422.

Правила:
- **Свёртка за проход:** больше 3 событий одного ключа одному админу → одно сообщение «Сбои задач: 12» и до 5 строк
  «сервер — действие», дальше «и ещё N, см. журнал». Предел 4096 символов, обрезка по строке.
- **Срок:** `expires_at = created_at аудита + BOT_NOTIFY_TTL_HOURS` (24 ч). На `claim` просроченная строка →
  `skipped expired`. В тексте — время события (`BOT_NOTIFY_TIMEZONE`).
- **Кому:** админ `is_active`, привязка живая (`telegram_id` задан, `blocked_at` пуст), ключ в `events`. Повторно
  проверяется на `claim` (`skipped no_recipient`).
- **Первый запуск:** курсор = момент миграции, истории нет.
- **Связь с панелью для бота:** только 2xx на `heartbeat` или `claim`. Неудача: сеть, 5xx, `PanelUnavailable`. 401 и
  429 — панель жива: «недоступна» не пишется.
- В тексте: управляющие и bidi-символы из имён вырезаются, ≤ 4096; нет адресов серверов, IP, почты, `telegram_id`,
  текстов ошибок задач — только имя сервера/пользователя, вид сбоя, время; в конце ссылка на журнал
  (`public_base_url` + `/audit?…`), если адрес задан.

## Модель и миграция

Одна ревизия после `c6e2f9a41d37` (`bot_outbox_broadcasts_settings`), именованные ограничения:

- `admin_telegram_links`: `id` UUID PK; `admin_id` UUID NOT NULL UNIQUE (FK admins CASCADE); `telegram_id` BIGINT NULL
  UNIQUE; `telegram_username` VARCHAR(64) NULL; `linked_at`, `blocked_at` TIMESTAMPTZ NULL; `events` JSONB NOT NULL;
  `code_hash` VARCHAR(64) NULL UNIQUE (SHA-256); `code_expires_at` TIMESTAMPTZ NULL; `created_at`, `updated_at`;
  CHECK `(telegram_id IS NULL) = (linked_at IS NULL)`, CHECK `(code_hash IS NULL) = (code_expires_at IS NULL)`.
- `bot_outbox`: `user_id` DROP NOT NULL; новые `admin_link_id` UUID NULL (FK CASCADE, индекс
  `ix_bot_outbox_admin_link`), `text` TEXT NULL, `format` VARCHAR(8) NULL (`plain`|`html`), `expires_at` TIMESTAMPTZ
  NULL, `notify_key` VARCHAR(32) NULL; `ck_bot_outbox_kind` пересоздаётся с `notify`; новый `ck_bot_outbox_recipient`:
  `(kind='notify' AND admin_link_id IS NOT NULL AND user_id IS NULL AND text IS NOT NULL AND format IS NOT NULL)
  OR (kind<>'notify' AND user_id IS NOT NULL AND admin_link_id IS NULL)`. Приоритет `PRIORITY_NOTIFY = 0`.
- `bot_notify_cursor`: `id` SMALLINT PK CHECK (`id=1`), `cursor_at` TIMESTAMPTZ NOT NULL; миграция вставляет `now()`.
- `bot_notify_seen`: `audit_log_id` UUID PK (без FK: аудит чистит retention), `seen_at`; чистит правило: старше
  `2 × OVERLAP`.
- `bot_keys`: + `last_seen_local` BOOLEAN NULL.
- **Downgrade:** 1) `DELETE FROM bot_outbox WHERE kind='notify'`; 2) снять `ck_bot_outbox_recipient`, вернуть прежний
  `ck_bot_outbox_kind`; 3) удалить колонки, `user_id` SET NOT NULL (порядок важен); 4) удалить три таблицы и
  `bot_keys.last_seen_local`. Теряются привязки, курсор и очередь уведомлений — записать в `alembic/CLAUDE.md`.
- Модели: `src/modules/bot/models/admin_link.py`, `notify.py` (курсор, seen), поля в `models/outbox.py`
  (`OutboxKind.NOTIFY`), импорт в `alembic/env.py`.

## Событие `server_health_changed` (`src/modules/servers/health.py`)

- `ServerHealthService(session, actor)`: воркер передаёт `actor` задачи (`src/infrastructure/workers/worker.py`),
  ручная проверка — от имени админа, плановая — `SYSTEM_ACTOR`.
- В `_save`: прежний статус перечитывается под `FOR UPDATE` (`with_for_update` + `populate_existing`, как в
  `src/modules/servers/repository.py`) — два параллельных health не пишут два события; при `old != new` —
  `AuditAction.SERVER_HEALTH_CHANGED`, сущность `server`, `meta {from, to, reason}`; `reason` только при `to=down`:
  `unreachable` (`SSHConnectionError`, `SSHTimeoutError`) или `ssh_error` (вход, ключ хоста); текста исключения нет.
  Запись — в том же commit.
- Имени сервера в `meta` нет: текст берёт имя при сборке (сервер удалён → «сервер <id[:8]>»).

## Сборщик (`src/modules/bot/notify.py`, правило `bot_notify`)

- Правило планировщика, `BOT_NOTIFY_INTERVAL_SECONDS` (15). Одновременность — Redis-ключ правила и
  `pg_advisory_xact_lock` в транзакции.
- Одна транзакция: курсор `FOR UPDATE` → события (`created_at >= cursor_at − OVERLAP`, `id NOT IN bot_notify_seen`,
  порядок `created_at, id`, `LIMIT BOT_NOTIFY_BATCH`) → фильтр каталога по `meta` → живые привязки и их ключи →
  группировка по (привязка, ключ) и тексты → `INSERT bot_outbox` (`kind=notify`, `format=plain`, `expires_at`) →
  `INSERT bot_notify_seen` (все прочитанные id, включая отфильтрованные) → `cursor_at = max(created_at)` прочитанных
  → чистка `seen` → commit.
- Нет привязок → только курсор и `seen`.
- `AuditLogRepository.list_for_notify(actions, since, exclude_seen, limit)` (индекс `ix_audit_logs_created_at_id`).

## Протокол Bot API

- `POST /outbox/claim` `{limit, kinds?}`: без `kinds` — `news`, `config`. Строки `notify`: просрочена →
  `skipped expired`; привязка без `telegram_id` / с `blocked_at` / админ неактивен → `skipped no_recipient`|`blocked`;
  иначе `{id, kind:"notify", telegram_id, text, format}`; `telegram_id` из `admin_telegram_links` (ветка в
  `OutboxService.claim`).
- `POST /outbox/ack`: `blocked` у `notify` → `admin_telegram_links.blocked_at`, не `vpn_users.bot_blocked_at`; счётчики
  рассылок не трогаются.
- `POST /heartbeat`: запрос + необязательное `local: bool`; ответ `{notify: {panel_down_after_seconds, chats:[…]}}`,
  `chats` = живые привязки активных админов с `panel_down` в `events`.
- `POST /admin-links` `{code, telegram_id, telegram_username?}`: 200 `{admin_name}`; 409 `reason`: `link_invalid`,
  `link_expired`, `telegram_taken`; успех: `telegram_id`, `linked_at`, сброс `blocked_at` и кода; повторная привязка
  того же админа заменяет Telegram. Лимит неудач по ключу `BOT_ADMIN_LINK_FAILURES` (10 за 600 с) — до сервиса.
  Одна зависимость `require_bot_key`, маршрут — в `BOT_API_ROUTES` теста.

## Бот (`bot/src/tollwire_bot/`)

- `panel.py`: `claim(limit, kinds)`; `OutboxItem` + `text`, `format`, `__repr__` без текста; `heartbeat(...) ->
  NotifyConfig` (сбой разбора → прежние значения); `admin_link(...)`; наблюдатель связи (`on_ok` при 2xx на
  heartbeat/claim, `on_fail` при `PanelUnavailable`/5xx).
- `outbox.py`: вид `notify` — `send_message(text, parse_mode="HTML" if html else None)`, превью выключено, журнал как у
  `news`.
- `commands.py`: `/admin <код>` и `/start <код в 32 символа>` → `admin_link`; диалог регистрации сбрасывается; код в
  лог не попадает (дополнить маску `RedactingFormatter`, включая `start=`).
- Новый `panel_watch.py`: цикл раз в 15 с; состояние в SQLite (`last_ok_at`, `down_notified_at`, `chats`,
  `threshold`) переживает рестарт; `now − last_ok_at >= threshold` и ещё не сообщали → «Панель не отвечает N мин»;
  повтор не чаще раза в 30 мин (`PANEL_WATCH_REPEAT_SECONDS` 1800); первый успех после сообщения → «Панель снова
  отвечает»; на пустом диске `last_ok_at` = старт; `chats` пуст → только лог; отправка ≤ 1/с в чат, 429 — пауза, 403 —
  чат убирается до следующего heartbeat.
- `heartbeat.py`: передаёт `local`, сохраняет `NotifyConfig`. `texts.py`: новые тексты; `__version__` → 0.3.0.

## Админский API (`/api/v1/bot-admin`)

| Метод и путь | Право | Что |
|---|---|---|
| `GET /me/telegram` | `bot:notify` | `{linked, telegram_username, linked_at, blocked_at, events, code_pending_until}` |
| `POST /me/telegram/code` | `bot:notify` | 201 `{code, link, expires_at}` (10 мин, `BOT_ADMIN_LINK_CODE_TTL_SECONDS`); прежний код гаснет; `no-store` |
| `PUT /me/telegram/events` | `bot:notify` | `{events}` по каталогу |
| `DELETE /me/telegram` | `bot:notify` | отвязать, очередь уходит каскадом; повтор — 204 |
| `POST /me/telegram/test` | `bot:notify` | 202 `{outbox_id}`; 409 `not_linked`, `bot_blocked` |
| `POST /broadcasts/test` | `bot:manage` | `{text}` (валидатор Telegram HTML) → 202 `{outbox_id}` себе, `format=html` |
| `GET /notify-events` | `bot:read` | каталог |
| `GET /admin-links` | `bot:read` | кто получает (без `telegram_id`) |
| `GET /outbox/{id}` | `bot:read` | уже есть |

- `BotSettingsUpdate` + `panel_down_after_seconds` (60..86400) — колонка уже есть.
- `GET /status` + `last_seen_local`, `notify_capable`, `admin_links`.
- **RBAC:** `Permission.BOT_NOTIFY` во все три роли, строка в `src/modules/auth/CLAUDE.md`; `test_rbac.py`: правило
  «записи — `bot:manage`» допускает `bot:notify` только для `/me/telegram*`; `bot_critical()` + `…/me/telegram/code`
  и `…/broadcasts/test`.
- **Лимиты:** `BOT_ADMIN_LINK_CODES_PER_ADMIN` 10/600, `BOT_NOTIFY_TESTS_PER_ADMIN` 10/600.

## Аудит

- Новые действия: `server_health_changed` (`{from,to,reason?}`), `bot_admin_link_code_created`,
  `bot_admin_link_created` (актор — бот; `{admin_id, replaced}`), `bot_admin_link_removed`,
  `bot_admin_link_events_updated`, `bot_notify_test_queued`; сущность `AuditEntity.ADMIN_TELEGRAM_LINK`.
- В `meta` нет: кода, `telegram_id`, `telegram_username`, текста пробной новости.
- Сами уведомления и «заблокировал бота» в аудит не пишутся; `bot_admin_link_*` в каталоге нет.
- Фронтенд: `AUDIT_ACTIONS`, `audit.actions.*` (ru, en), `DETAIL_LABELS` — `from`, `to`, `reason`, `replaced`.

## Настройки (`core/config.py`, `.env*.example`)

`BOT_NOTIFY_INTERVAL_SECONDS` 15, `BOT_NOTIFY_OVERLAP_SECONDS` 2100 (валидатор: ≥ `STALE_JOB_TIMEOUT_SECONDS` + 300),
`BOT_NOTIFY_BATCH` 500, `BOT_NOTIFY_TTL_HOURS` 24, `BOT_NOTIFY_TIMEZONE` UTC (проверить `tzdata` в образе),
`BOT_ADMIN_LINK_CODE_TTL_SECONDS` 600, `BOT_ADMIN_LINK_FAILURES`/`_WINDOW_SECONDS` 10/600; для бота
`PANEL_WATCH_REPEAT_SECONDS` 1800.

## Фронтенд

- Вкладка `/bot/notify` «Мои уведомления» (при `bot:notify`): состояние привязки; диалог «Привязать» (ссылка, код,
  отсчёт, опрос `GET /me/telegram` раз в 2 с до `linked`, `gcTime: 0`); галочки событий по группам; «Проверить»
  (опрос `GET /outbox/{id}`); «Отвязать» через `ConfirmDialog`; предупреждения `!notify_capable` («обновите бота») и
  `last_seen_local`; свёрнутый список «Кто получает».
- «Настройки»: порог «панель недоступна» (минуты 1..1440) с подсказкой про `last_seen_local`.
- «Рассылки»: «Отправить себе на пробу» (есть привязка и `bot:manage`).
- Словари `bot.json` (ru, en), `audit.json`, `shell.json`; после схем — `export_openapi` → `npm run gen:api`.

## Шаги

- [ ] 1. Красные тесты (`tester`), коммит `tests(bot): красные тесты этапа 25.5`.
- [ ] 2. `core`: настройки, лимиты, маска `start=`; `.env*.example`.
- [ ] 3. Модели, миграция, `alembic/env.py`.
- [ ] 4. `servers/health.py`: `actor`, блокировка, `server_health_changed`; воркер передаёт `actor`; `AuditAction`/`AuditEntity`.
- [ ] 5. Сервисы бота: `AdminLinkService`, `notify_catalog.py`, `notify_texts.py`, `NotifyCollector`, `claim`/`ack`/heartbeat с `notify`; `AuditLogRepository.list_for_notify`; правило в `scheduler.py`.
- [ ] 6. Роутеры: Bot API, админский API, `Permission.BOT_NOTIFY`, настройки.
- [ ] 7. Бот: `panel.py`, `outbox.py`, `commands.py`, `panel_watch.py`, `storage.py`, `heartbeat.py`, `texts.py`, версия 0.3.0.
- [ ] 8. Фронтенд.
- [ ] 9. Документация: `src/modules/bot/CLAUDE.md`, `docs/bot/api.md`, `docs/bot/notify.md`, `docs/bot/tests.md`, `bot/CLAUDE.md`, `docs/bot-service/*` (+ `panel-watch.md`), `servers`, `audit`, `auth`, `alembic`, `workers`, `frontend` CLAUDE.md, INSTALL, CHANGELOG.

## Тесты

- **unit:** каталог (фильтр `server_health_changed`: ok→down да, down→ok да, ok→degraded нет, null→ok нет, null→down да);
  тексты (≤ 4096, свёртка, вырезание bidi, нет IP/почты/`telegram_id`); схема `events` (неизвестный ключ → 422);
  валидатор overlap; маска `start=`.
- **services:** `test_server_health_event` (одно событие на смену, параллельные `_save`, актор, `reason`);
  `test_bot_notify` (адресаты, дедупликация, **событие с `created_at` раньше курсора, закоммиченное позже — доставлено
  следующим проходом**, нет истории до первого запуска, свёртка, `LIMIT`, чистка `seen`, курсор без привязок);
  `test_outbox` (`kinds`, `expired`, `no_recipient`, `ack blocked` → `admin_telegram_links`, CHECK получателя);
  `test_admin_links` (код: хеш, один раз, TTL; привязка: успех/просрочен/использован/`telegram_taken`/замена;
  каскад; heartbeat-чаты).
- **migrations:** цикл upgrade/downgrade с данными (строка `notify` удаляется до NOT NULL), CHECK, курсор, CASCADE.
- **integration:** `test_bot_notify_flow` (health → воркер → `bot_notify` → `claim(kinds)` через `PanelClient`:
  одно сообщение на падение, тишина при повторном down, одно на восстановление); `test_bot_auth`;
  `test_bot_client_contract`; `test_rbac`; `test_forced_password_change`; аудит без кода и `telegram_id`; кода нет в `caplog`.
- **bot/tests:** `test_panel_watch` на фальшивых часах (порог, повтор через 30 мин, восстановление, 401/429 не
  недоступность, рестарт без дублей, пустой `chats`); `test_outbox` (`notify` plain/html); `test_commands`
  (`/admin`, 32 против 24 символов, отказы, код не в логе); `test_heartbeat`; `test_texts`.
- **frontend (vitest):** привязка, события, проба, отвязка, предупреждения, порог, «себе на пробу», viewer, сверка
  ключей `i18n`.

## Риски

- **Поздний commit** → перекрытие и `seen`, тест с открытой транзакцией. Транзакция длиннее перекрытия (закрыта
  `stale_jobs` как FAILED) может быть пропущена — компромисс.
- **Шквал** → свёртка и `LIMIT`; при необходимости предел в час на админа (отдельно).
- **Ложные «сервер недоступен»** → `reason` в тексте; подтверждение двумя проверками позже.
- **Старый бот / новая панель** → `kinds`, срок 24 ч, предупреждение. **Новый бот / старая панель:** heartbeat
  отдаёт `{}`, `panel_watch` только пишет в лог.
- **Бот на сервере панели** не видит падения хоста → `last_seen_local` и подсказка.
- **Перехват кода** → TTL 10 мин, одноразовый, `no-store`, подтверждение с именем админа, аудит.
- **Утечки** → в тексте только имена и вид сбоя; `telegram_id` админов видит любой действующий ключ (как в 25.1);
  `state.sqlite3` 0600.
- **Блокировка в health** → `FOR UPDATE` на строку сервера на время commit; проверить, что выдача доступа не держит
  `vpn_servers` под `FOR UPDATE`.
- **Миграция** быстрая; downgrade сначала удаляет `notify`.
- **Живой прогон** шлёт сообщения реальным людям — только админам-участникам и с согласия; сборщик работает в
  воркере, dev-воркер не запускать при общем сервере с prod.

## Проверка

- `uv run ruff check --no-cache . && uv run mypy src`; тесты через `test-runner`, полный `pytest -n 4 -q` (менялся `alembic`).
- Из `bot/`: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`.
- Фронтенд: `npm run typecheck && npm run lint && npm test && npm run build`; `export_openapi` → `npm run gen:api`.
- `alembic upgrade head`, `downgrade -1`, снова `upgrade head` — с непустой очередью `notify`.
- Живой прогон (с согласия): привязка по ссылке и `/admin`; «Проверить»; недоступный сервер → одно сообщение, повтор
  тихо; закрыть SSH фаерволом → «не отвечает», открыть → «снова доступен»; `docker compose stop api` дольше порога →
  «панель недоступна», повтор через 30 мин, `start api` → «восстановлена», рестарт бота без дублей; бот 0.2.0 против
  новой панели; отвязка; Lighthouse на `/bot/notify`.
