# План: этап 25.4 — страница «Бот» и рассылки

ТЗ: [telegram-bot.md](telegram-bot.md) (разделы 4, 5, 8, 11, 12; этап 25.4). Готовые этапы: [25.1](telegram-bot-25-1.md),
[25.2](telegram-bot-25-2.md), [25.3](telegram-bot-25-3.md).

## Задача

Админ из панели видит бота (имя, последний опрос, версия), управляет ключами, приглашениями и регистрациями и делает
рассылки: новости (Telegram HTML) и ссылки на конфиг (одному человеку из карточки или массово, каждому новая ссылка).
Сообщения лежат в исходящей очереди `bot_outbox` в БД панели; бот забирает их через Bot API, сам соблюдает лимиты
Telegram и подтверждает итог. Готово, когда рассылка на 1000 получателей укладывается в лимит Telegram, а
заблокировавшие бота помечены.

Допущения: бот один (несколько ключей допустимы, UI под одного бота — ТЗ §13); персонализации текста новостей нет
(никаких `{name}`); SSH и задачи воркера рассылке не нужны.

## Не входит

Уведомления админу, `admin_telegram_links`, `POST /admin-links`, `notify_events`, `server_health_changed`, «панель
недоступна» на стороне бота, `settings_version` в heartbeat — всё это 25.5. Кроме того: отправка новости «себе на
пробу» (нужна привязка Telegram админа — 25.5), платные рассылки (`allow_paid_broadcast`), вложения и кнопки в
новостях, превью ссылок в новостях, ручной отзыв ссылки на конфиг админом.

## Развилки (решает пользователь)

- **Р1. Когда выпускать ссылку для рассылки конфигов.**
  - A — при выдаче сообщения боту (`claim`): токен живёт только в памяти запроса, в строке очереди — `user_id` и
    `config_link_id`. Пока бот не забрал сообщение, у человека работает прежняя ссылка. Цена: если ответ `claim`
    потерялся или подтверждение не дошло, повторная выдача выпускает ещё одну ссылку, и предыдущая гаснет (человек
    может получить два сообщения, первое с мёртвой ссылкой).
  - B — при постановке в очередь, токен в `bot_outbox` зашифрован (`ENCRYPTION_KEY`) до подтверждения. Повтор
    отправляет ту же ссылку. Цена: у всей тысячи получателей текущие ссылки гаснут сразу при постановке (а бот
    может лежать сутки); открытый секрет хранится в БД, при отмене рассылки токены надо отзывать.
  - **Рекомендация: A** — так и записано в рисках 25.3; токена в БД нет совсем.
- **Р2. Кто держит темп отправки.**
  - A — бот: ведро токенов на 20 сообщений/с (настройка бота, не больше 28), не чаще раза в секунду в один чат,
    глобальная пауза при 429 на `retry_after`. Панель ограничивает только размер пачки (≤100) и время аренды.
  - B — панель: окно в Redis на `claim`. Цена: панель не видит ни 429 Telegram, ни ответов бота людям в диалогах
    (они расходуют тот же лимит в 30/с).
  - **Рекомендация: A** — лимит принадлежит токену Telegram, а он есть только у бота (ТЗ §2).
- **Р3. Безопасность Telegram HTML.**
  - A — строгая проверка с отказом: свой разборщик на stdlib `html.parser.HTMLParser`, белый список тегов и
    атрибутов; ошибка — 422 с кодом и позицией, текст не меняется молча. Предпросмотр на фронтенде — обход дерева
    `DOMParser` с выводом React-элементов из белого списка, без `innerHTML`.
  - B — санитайзер, исправляющий текст (`nh3`): новая зависимость, отправляется не то, что видел админ.
  - C — только обычный текст: проще всего, но ТЗ просит HTML.
  - **Рекомендация: A.**
- **Р4. Счётчики рассылки.**
  - A — хранятся в `bot_broadcasts` (`sent/blocked/failed/skipped/cancelled`) и меняются в той же транзакции, что
    статус строки (`claim`, `ack`, `cancel`). Переживают чистку очереди.
  - B — агрегат по `bot_outbox` при чтении: расхождений быть не может, но после чистки старых строк итог
    пропадает.
  - **Рекомендация: A** + тест-инвариант «total = сумма итогов + открытые + выбывшие».
- **Р5. Где хранить настройки бота.**
  - A — таблица `bot_settings`, одна строка (`id = 1`, CHECK), как в ТЗ §4.
  - B — переменные окружения: админ не меняет их из UI.
  - C — общая таблица ключ-значение: нет типов и CHECK.
  - **Рекомендация: A.**
  - Подразвилка **Р5.1**: порог «панель недоступна» — колонка заводится сейчас (одна миграция), а поле в UI
    появляется (A) сейчас с пометкой «работает с 25.5» или (B) в 25.5, когда бот начнёт его читать.
    **Рекомендация: B** — не показывать настройку, которая ничего не делает.
- **Р6. Страница «Бот» видна, если есть хотя бы один ключ.**
  - A — каркас при `can("bot:read")` запрашивает `GET /bot-admin/status` (`has_keys`, `staleTime` 5 мин). Пункт
    меню виден при `has_keys`, а суперадмину (`bot:keys`) — всегда, иначе первый ключ из панели не создать. Прямой
    заход на `/bot` без ключей у не-суперадмина показывает «Бот не подключён».
  - B — флаг `features.bot` в `/auth/me`: модуль `auth` начинает зависеть от `bot`.
  - **Рекомендация: A.** «Есть ключ» — любой, включая отозванные: история регистраций остаётся доступной.
- **Р7. Навигация внутри страницы.**
  - A — подмаршруты `/bot` (состояние и ключи), `/bot/invites`, `/bot/registrations`, `/bot/broadcasts`,
    `/bot/settings`: каждый — ленивый чанк, ссылки на разделы можно пересылать, `NavLink` сам ставит
    `aria-current`.
  - B — одна длинная страница с секциями.
  - C — Radix Tabs: новая зависимость.
  - **Рекомендация: A.**
- **Р8. Снятие пометки «заблокировал бота».**
  - A — автоматически, когда человек сам пишет боту: панель обнуляет `bot_blocked_at` в `GET /users/by-telegram/{id}`
    и в `POST …/config-link` (обращения, которые бот делает по `/start`, `/status`, `/config`). Это
    идемпотентная запись в обработчике GET.
  - B — только кнопкой админа: админ не знает, разблокировал ли человек бота.
  - C — отдельный вызов Bot API из промежуточного слоя бота: лишний трафик.
  - **Рекомендация: A.**
- **Р9. Получатели «выбранные».**
  - A — выбор с поиском в форме рассылки (`GET /bot-admin/recipients?search=`: только люди с Telegram, отметка
    «заблокировал»).
  - B — флажки в списке `/users` и действие «Разослать»: правка чужого экрана.
  - **Рекомендация: A.**

## Принятые решения (при рекомендованных вариантах)

- **Р1-A, Р2-A, Р3-A, Р4-A, Р5-A (Р5.1-B), Р6-A, Р7-A, Р8-A, Р9-A.**
- `kind`: `news`, `config` (вместо `broadcast` из ТЗ §4); `notify` вместе со своим получателем добавит миграция
  25.5. Отправка — без воркера и SSH: постановка в очередь — один `INSERT … SELECT` в запросе API.
- В строке очереди — `user_id` (FK CASCADE), а не снимок `telegram_id`: адрес берётся из `vpn_users` при `claim`.
  Удалённый человек выбывает из рассылки сам и показывается как «выбыли» = total − Σ.
- Текст новости хранится один раз (`bot_broadcasts.text`); у строк `config` текста нет — сообщение собирает бот из
  своего шаблона (обычный текст без `parse_mode`).
- Аренда пачки: `lease_id` (UUID на каждый `claim`) + `claimed_until` (60 с). `ack` принимается, только если
  `lease_id` строки совпадает (просроченное, но ещё не переданное другому подтверждение тоже принимается); иначе id
  уходит в `ignored`.

## Модель и миграция

Одна ревизия после `b4f1c8e27a69` (`config_links`), именованные ограничения:

- `vpn_users`: + `bot_blocked_at` TIMESTAMPTZ NULL.
- `bot_keys`: + `last_claim_at` TIMESTAMPTZ NULL (UI: «бот не забирает очередь» — старая версия бота или сбой).
- `bot_settings`:
  - поля: `id` SMALLINT PK CHECK (`id = 1`); `default_protocol_id` UUID NULL (FK protocols SET NULL); `default_days`
    INT NOT NULL 30 (CHECK 1..3650); `invite_valid_days` INT NOT NULL 7 (CHECK 1..365); `panel_down_after_seconds`
    INT NOT NULL 300 (CHECK 60..86400; использует 25.5); `updated_at`; `updated_by` (FK admins SET NULL);
  - миграция вставляет строку `id = 1`.
- `bot_broadcasts`:
  - поля: `id`; `kind` VARCHAR(16) CHECK (`news`, `config`); `text` TEXT NULL — CHECK `kind = 'config' OR text IS NOT
    NULL`; `target` VARCHAR(16) CHECK (`all`, `selected`, `protocol`); `protocol_id` (FK SET NULL); `request_id`
    UUID UNIQUE (повторный POST с тем же id возвращает ту же рассылку); `created_by` (FK admins SET NULL);
    `created_at`; `cancelled_at`; `finished_at`;
  - счётчики INT NOT NULL 0: `total`, `sent`, `blocked`, `failed`, `skipped`, `cancelled`.
- `bot_outbox`:
  - поля: `id` UUID; `kind`; `broadcast_id` (FK bot_broadcasts CASCADE, NULL — индивидуальная отправка); `user_id`
    NOT NULL (FK vpn_users CASCADE); `access_grant_id` NULL (FK access_grants SET NULL; доступ, по которому
    рассылка «по протоколу»); `status` VARCHAR(16) CHECK (`pending`, `claimed`, `sent`, `blocked`, `failed`,
    `skipped`, `cancelled`); `priority` SMALLINT (0 — индивидуальное, 10 — рассылка); `not_before`; `lease_id`;
    `claimed_until`; `claimed_by_key_id` (FK bot_keys SET NULL); `attempts` INT 0; `error` VARCHAR(32) (код из
    перечня, не текст); `config_link_id` (FK config_links SET NULL); `created_at`; `updated_at`; `sent_at`;
  - индексы: частичный `ix_bot_outbox_ready (priority, created_at) WHERE status IN ('pending','claimed')`;
    `ix_bot_outbox_broadcast_status (broadcast_id, status)`; `ix_bot_outbox_lease (lease_id)`;
    `uq_bot_outbox_broadcast_user (broadcast_id, user_id)`; частичный уникальный
    `uq_bot_outbox_open_config (user_id) WHERE kind = 'config' AND status IN ('pending','claimed')` — две живые
    отправки ссылки одному человеку гасили бы друг друга.
- Downgrade: удаляет две таблицы, `bot_settings` и две колонки. Теряются очередь, итоги рассылок и пометки
  «заблокировал» — записать в `alembic/CLAUDE.md`.

## Очередь: протокол Bot API

**`POST /outbox/claim`** `{limit: 1..100}`. Ответ: `{lease_id, lease_until, items: [...], texts: {broadcast_id:
text}}`, заголовок `Cache-Control: no-store`.

Транзакция 1:
- выбрать строки: `pending` с `not_before <= now`, либо `claimed` с `claimed_until < now` (такой строке
  `attempts + 1`);
- порядок `priority, created_at`; `FOR UPDATE SKIP LOCKED LIMIT n` — тот же приём, что у `list_provisioning`
  (`src/modules/bot/repository.py`);
- `attempts >= BOT_OUTBOX_MAX_ATTEMPTS` → `failed too_many_attempts`;
- у человека нет `telegram_id` → `skipped no_telegram`; стоит `bot_blocked_at` → `blocked`;
- остальное → `claimed`, `lease_id`, `claimed_until = now + 60 с`, `claimed_by_key_id`;
- `bot_keys.last_claim_at = now`; commit.

Затем каждая строка `config` — своя транзакция:
- `ConfigLinkService.stage_issue(user_id, grant_id, source="outbox", meta)` (новый метод: то же, что `_issue` в
  `src/modules/bot/config_links.py`, но по `user_id`; при заданном гранте берётся его клиент; без commit — по образцу
  `stage_*` из 25.1 Р2-A). `issue_for_telegram` = `stage_issue` + commit — поведение `/config` не меняется;
- в строку пишется `config_link_id`; аудит `config_link_created` `{credential_id, user_id, source: outbox,
  broadcast_id?, replaced}`; commit;
- отказы:
  - `no_access`, `config_unavailable`, `not_configured` → `skipped` с этим кодом;
  - `not_ready` → обратно `pending`, `not_before = now + 60 с`; если строке больше 30 мин — `skipped not_ready`.
- Токен — только в ответе.

Элемент ответа:
- `news`: `{id, kind, telegram_id, broadcast_id}`;
- `config`: `{id, kind, telegram_id, config_url, valid_until}`.

**`POST /outbox/ack`** `{lease_id, results: [{id, status: sent|blocked|failed|retry, error?, retry_after?}]}`, до 100
результатов. Транзакция:
- строки `WHERE id IN … AND lease_id = :lease_id AND status = 'claimed' FOR UPDATE`;
- `sent` → `sent_at`;
- `blocked` → `vpn_users.bot_blocked_at = now`;
- `failed` → `error` из перечня;
- `retry` → `pending`, `lease_id = NULL`, `not_before = now + max(retry_after, 5)`; `attempts + 1`, кроме
  `error = flood`;
- счётчики рассылки — `UPDATE … SET sent = sent + :n`, одной строкой на рассылку;
- у рассылки не осталось открытых строк → `finished_at`.

Ответ `{applied: [...], ignored: [...]}`. Повтор того же `ack` ничего не меняет.

Коды `error` от бота — только перечень: `blocked`, `chat_not_found`, `bad_request`, `network`, `telegram_5xx`,
`flood`, `too_many_attempts`. Текст Telegram в панель не передаётся.

## Бот: отправитель (`bot/src/tollwire_bot/outbox.py`)

- Цикл: `claim(limit=OUTBOX_BATCH)` раз в 5 с; полная пачка — сразу следующая.
- Перед каждой отправкой:
  - ведро токенов `OUTBOX_RATE_PER_SECOND` (по умолчанию 20, 1..28): запас под ответы людям в диалогах;
  - не чаще 1 с в один чат;
  - если до `lease_until` меньше 5 с — не отправлять, остаток вернуть `retry`.
- Отправка:
  - `news` — `send_message(chat_id, text, parse_mode="HTML")`; превью ссылок выключено по умолчанию, как в 25.3;
  - `config` — шаблон `broadcast_config` из `texts.py` (адрес, срок, «прежняя ссылка больше не работает»), без
    `parse_mode`.
- Исключения aiogram:
  - `TelegramRetryAfter` → пауза всего отправителя на `retry_after` и повтор того же сообщения; не укладывается в
    аренду → `retry` с `error = flood`;
  - `TelegramForbiddenError` → `blocked`;
  - `TelegramBadRequest`: «chat not found» → `failed chat_not_found`, прочее → `failed bad_request`;
  - `TelegramNetworkError` → `retry network`; `TelegramServerError` → `retry telegram_5xx`.
- Журнал в `SqliteStorage`, таблица `outbox_journal (id, lease_id, kind, status, error, at)`: запись после ответа
  Telegram, удаление после успешного `ack`. Подтверждения отправляются пачкой каждые 25 итогов или 2 с и при
  SIGTERM; после рестарта их досылает старый `lease_id`. Если строка `news`, уже записанная как `sent`, пришла в
  новой аренде, она подтверждается без повторной отправки. Строка `config` отправляется всегда: в ней новая ссылка.
  Записи старше 24 ч чистит `purge`.
- `panel.py`: `claim(limit) -> Claim`, `ack(lease_id, results)`. `OutboxItem.__repr__` без адреса.
- `__version__` → 0.2.0. Фоновая задача — в `__main__.py`.
- Расчёт: 1000 сообщений при 20/с — около 50 с. Запросов к панели при пачке 50 — около 20 `claim` и 40 `ack`, в
  лимите `BOT_KEY_MAX_REQUESTS` (300 за 60 с).

## Рассылки (панель, `src/modules/bot/broadcasts.py`, `outbox.py`)

- Получатели:
  - `all` — у человека есть `telegram_id` и нет `bot_blocked_at`;
  - `selected` — до 1000 `user_id`, к ним те же фильтры;
  - `protocol` — живой грант на протоколе (`inactive_reason(...) is None`); в строку пишется `access_grant_id`.
  - У `config` только `selected` и `protocol`.
- Предел — `BOT_BROADCAST_MAX_RECIPIENTS` (5000), больше → 422.
- Постановка: `INSERT … SELECT … ON CONFLICT DO NOTHING`; `total` — число вставленных строк. Для `config` заранее
  проверяется `public_base_url` (409 `not_configured`).
- `POST /broadcasts/audience` — тот же отбор без записи: `{recipients, without_telegram, blocked}`. Кнопка
  «Отправить N получателям» и подтверждение показывают это число.
- Отмена: `pending` → `cancelled`, `cancelled_at`, счётчик. Строки в аренде доходят (до одной пачки после отмены).
  Состояние рассылки: `cancelled_at` → «отменена»; есть открытые строки → «идёт»; иначе — «завершена».
- Индивидуальная ссылка — `POST /bot-admin/users/{user_id}/config-link` → 202 `{outbox_id, status}`:
  - заранее проверяются `no_telegram`, `bot_blocked`, `not_configured`, а выбор клиента — без выпуска (`no_access`,
    `config_unavailable`); все — 409 с `reason`;
  - открытая строка уже есть → возвращается она;
  - приоритет 0: не ждёт большую рассылку.
- Telegram HTML (`src/modules/bot/telegram_html.py`, вызывается из валидатора схемы, `PydanticCustomError`):
  - теги `b`, `strong`, `i`, `em`, `u`, `ins`, `s`, `strike`, `del`, `tg-spoiler`, `span class="tg-spoiler"`,
    `a href` (только `http`/`https`), `code`, `pre`, `pre > code class="language-…"`, `blockquote [expandable]`;
    `tg-emoji` и `tg-time` не допускаются;
  - теги вложены правильно; внутри `a` нет `a`, внутри `pre`/`code` нет разметки;
  - именованные сущности — только `&lt; &gt; &amp; &quot;`, числовые — любые; голые `<` и `&` — ошибка;
  - видимый текст 1..4096 единиц UTF-16 после разбора; исходник ≤ 16384 символов;
  - запрещены управляющие символы (кроме `\n`, `\t`) и двунаправленные переопределения U+202A–202E, U+2066–2069;
    ZWJ (U+200D) разрешён — без него ломаются эмодзи;
  - коды ошибок `telegram_html_tag/attr/entity/nesting/length/empty`, в `ctx` — позиция. Обработчик 422 не
    возвращает `input` (`src/main.py`).
  - Набор примеров `frontend/src/lib/telegramHtml.cases.json` читают и pytest, и vitest — проверка и предпросмотр
    не разойдутся.

## Настройки

- `GET /bot-admin/settings` (`bot:read`), `PUT /bot-admin/settings` (`bot:manage`). Аудит `bot_settings_updated` с
  `{changes: {поле: [было, стало]}}`, как у `admin_updated`.
- `InviteCreate`: `protocol_id`, `days`, `expires_at` становятся необязательными; недостающее берётся из настроек
  (`expires_at = now + invite_valid_days`). Нет протокола ни в запросе, ни в настройках → 409 `no_default_protocol`.
  Консольный `bot_invite` по-прежнему требует всё явно.

## Админский API: что добавить (`/api/v1/bot-admin`)

| Метод и путь | Право | Что |
|---|---|---|
| `GET /status` | `bot:read` | `{has_keys, active_keys, bot_username, last_seen_at, last_seen_version, stale (нет heartbeat дольше BOT_STALE_AFTER_SECONDS=120), last_claim_at, outbox: {pending, claimed}}`; ключ — последний замеченный действующий (как `latest_bot_username`) |
| `GET /settings`, `PUT /settings` | `bot:read` / `bot:manage` | см. выше |
| `GET /protocols` | `bot:read` | протоколы для приглашения и рассылки (тот же запрос, что `bot_invite protocols`): id, сервер, тип |
| `GET /recipients?search=&limit=` | `bot:read` | люди с Telegram: id, имя, `telegram_username`, `bot_blocked_at` |
| `GET /invites`, `GET /registrations`, `GET /broadcasts` | `bot:read` | курсор `before_created_at`/`before_id`, `limit ≤ 100` (как `GET /audit-logs`); у `InviteRead` + `used_by_user_id`, `used_by_name` |
| `POST /broadcasts/audience` | `bot:manage` | число получателей |
| `POST /broadcasts` | `bot:manage` | `{request_id, kind, text?, target, protocol_id?, user_ids?}` → 201 |
| `GET /broadcasts/{id}` | `bot:read` | счётчики и состояние |
| `POST /broadcasts/{id}/cancel` | `bot:manage` | 200; повтор ничего не меняет |
| `POST /users/{user_id}/config-link` | `bot:manage` | 202 |
| `GET /outbox/{id}` | `bot:read` | статус и `error` одной строки — опрос из карточки пользователя |

- `VPNUserRead`: + `telegram_id`, `telegram_username`, `bot_blocked_at`.
- Права не добавляются: все записи (кроме ключей) — `bot:manage`, чтение — `bot:read`. Это правило теста
  `tests/integration/test_rbac.py`; матрицу ролей не трогаем.
- `bot_critical()` += `POST /api/v1/bot-admin/broadcasts` и `POST /api/v1/bot-admin/users/{user_id}/config-link`
  (оба `BOT_MANAGE`).
- Bot API: + `POST /outbox/claim`, `POST /outbox/ack` (ровно одна зависимость `require_bot_key`; в
  `BOT_API_ROUTES` теста).

## Аудит

- Новые действия:
  - `bot_broadcast_created` (сущность `bot_broadcast`): `{kind, target, protocol_id?, recipients, text_length}`;
  - `bot_broadcast_cancelled`: `{cancelled}`;
  - `bot_config_link_queued` (сущность `vpn_user`): `{outbox_id}`;
  - `bot_settings_updated` (сущность `bot_settings`): `{changes}`;
  - у `config_link_created` новое значение `source: outbox` и `broadcast_id`.
- В `meta` нет: текста новости, `telegram_id`, почты, токена, `config_url`, списка `user_ids`.
- Пометка «заблокировал» — не действие админа: в аудит не пишется, видна в карточке и в итогах рассылки.
- Фронтенд: `AUDIT_ACTIONS` и `audit.actions.*` в обоих словарях; в `DETAIL_LABELS` — `kind`, `target`,
  `recipients`, `cancelled`.

## Лимиты (`core/config.py`, `.env*.example`)

- `BOT_BROADCASTS_PER_ADMIN` — 10 за 3600 с.
- `BOT_CONFIG_SENDS_PER_ADMIN` — 60 за 600 с.
- `BOT_BROADCAST_MAX_RECIPIENTS` — 5000.
- `BOT_OUTBOX_LEASE_SECONDS` — 60.
- `BOT_OUTBOX_MAX_ATTEMPTS` — 5.
- `BOT_OUTBOX_RETENTION_DAYS` — 30: правило `retention` удаляет завершённые строки; счётчики рассылки остаются (Р4-A).
- Бот: `OUTBOX_RATE_PER_SECOND` (20), `OUTBOX_BATCH` (50).
- `core/log.py`: шаблон адреса `…/c#<токен>` маскируется (ответ `claim` может попасть в лог при ошибке).

## Фронтенд

- Меню: `AppShell` + пункт «Бот» по Р6-A.
- Маршруты `/bot/*` по Р7-A, ленивый `pages/bot/`:
  - «Состояние»:
    - `@имя`, последний опрос, версия; при `stale` — предупреждение `Notice`; при открытой очереди и старом
      `last_claim_at` — «бот не забирает очередь (обновите бота)»;
    - ключи: создать — диалог «показан один раз», копирование, `gcTime: 0`, сброс при закрытии; отозвать —
      `ConfirmDialog` «бот перестанет работать сразу». Кнопки ключей — только `bot:keys`.
  - «Приглашения»:
    - создать: протокол и срок подставлены из настроек, «действует до» — 1/7/30 дней или своя дата, заметка;
    - результат: ссылка `t.me`, «Скопировать», запасной вариант — поле только для чтения, как на `/c`; без имени
      бота — код и подсказка;
    - список: «ждёт / использовано: имя (ссылка на карточку) / истекло / отозвано», отозвать.
  - «Регистрации»: фильтр по статусу; у `failed` — `<details>` с ошибкой и «Повторить» (`bot:manage`).
  - «Рассылки»:
    - форма: вид (новость / ссылки на конфиг); текст — `textarea`, счётчик «N из 4096» в `aria-describedby`,
      предпросмотр (ссылки в предпросмотре не кликабельны);
    - кому: все / протокол / выбранные (Р9-A); число получателей; подтверждение;
    - список рассылок: `<progress>` с подписью и счётчики «отправлено / заблокировали / ошибки / пропущено /
      отменено», «Отменить»; опрос раз в 2 с, пока есть рассылки в работе (`refetchInterval(query)` → `false`).
  - «Настройки»: протокол и срок по умолчанию, «приглашение действует, дней»; порог — в 25.5 (Р5.1-B).
- Карточка пользователя:
  - строка «Telegram: @ник», при пометке — «заблокировал бота ДД.ММ»;
  - кнопка «Отправить ссылку в Telegram» (`bot:manage` и есть `telegram_id`): при пометке «заблокировал» —
    неактивна с объяснением; после нажатия — опрос `GET /outbox/{id}` раз в 2 с до итога и `Notice` с итогом;
    причины 409 — свои тексты.
- Состояния каждого раздела:
  - загрузка — «Загрузка…» с `aria-busy`;
  - ошибка — `Notice`, текст выбирается по коду или `reason`; `ApiError.detail` не показывается;
  - 429 — «слишком часто, попробуйте через N с»;
  - пусто — своё пустое состояние;
  - viewer видит всё без кнопок.
- Словари: `bot.json` (ru, en; плюральные ключи для счётчиков), правки `shell.json`, `users.json`, `audit.json`.
  CSS-модули, токены дизайна; Lighthouse «Доступность» 100 на всех вкладках.

## Шаги

- [x] 1. Красные тесты (`tester`), коммит `tests(bot): красные тесты этапа 25.4`.
- [x] 2. `core`: лимиты, маска `/c#`; `.env*.example`. Коммит.
- [x] 3. Модели (`models/{settings,broadcast,outbox}.py`, поля `vpn_users`, `bot_keys`), миграция, импорт в
  `alembic/env.py`. Коммит.
- [x] 4. `telegram_html.py` и схемы; набор примеров. Коммит.
- [x] 5. Сервисы: `BotSettingsService`, значения по умолчанию в `BotInviteService.create`;
  `ConfigLinkService.stage_issue`; `OutboxService` (`claim`, `ack`, постановка, `audience`, `cancel`,
  индивидуальная отправка); снятие `bot_blocked_at` (Р8-A); репозитории. Коммит.
- [x] 6. Роутеры: Bot API `outbox/*`, админский API, `VPNUserRead`; `AuditAction`/`AuditEntity`; правило
  `retention`. Коммит.
- [x] 7. Бот: `panel.py`, `outbox.py`, `storage.py` (журнал), `texts.py`, `config.py`, `__main__.py`, версия; тесты.
  Коммит.
- [ ] 8. Фронтенд: `export_openapi` → `gen:api`, меню, `pages/bot/*`, карточка пользователя, словари,
  `AUDIT_ACTIONS`, тесты. Коммит.
- [ ] 9. Документация: `src/modules/bot/CLAUDE.md`, `docs/bot/api.md`, новый `docs/bot/outbox.md`,
  `docs/bot/tests.md`, `bot/CLAUDE.md`, `docs/bot-service/{panel-client,tests}.md` и новый
  `docs/bot-service/outbox.md`, `vpn_users`, `audit`, `alembic`, `core`, `infrastructure/workers` (retention),
  `frontend/CLAUDE.md`, `docs/frontend/structure.md`, INSTALL (переменные бота, обновлять бота вместе с панелью),
  CHANGELOG. Коммит `docs: …`.

## Тесты

- **unit:**
  - `telegram_html` на наборе примеров: каждый тег и атрибут, `javascript:` в `href`, неизвестный тег, `<a>` в `<a>`,
    незакрытый тег, `&nbsp;`, голые `&` и `<`, длина 4096/4097 в UTF-16 (эмодзи = 2), пустой текст, RLO, ZWJ;
  - схемы рассылки: `kind`/`target`/`protocol_id`/`user_ids`, предел числа получателей;
  - маска `/c#…` в `core/log`.
- **services/test_outbox.py:**
  - постановка (`all`/`selected`/`protocol`; без Telegram и заблокировавшие не попадают; дубль `request_id`; вторая
    открытая ссылка на конфиг не ставится);
  - `claim` по приоритету; два параллельных `claim` получают непересекающиеся строки;
  - аренда истекла → строка выдаётся снова, `attempts + 1`; `ack` с чужим `lease_id` → `ignored`; повтор `ack`
    ничего не меняет; просроченный, но не переданный дальше `ack` принят;
  - `blocked` ставит `bot_blocked_at`, следующие строки этого человека → `blocked` без выдачи;
  - `retry`/`flood` и `not_before`; предел попыток;
  - `config`: ссылка выпускается только при `claim`; ни в одной текстовой колонке `bot_outbox`, `bot_broadcasts`,
    `audit_logs` нет токена и `config_url`;
  - отзыв доступа между постановкой и `claim` → `skipped no_access`; `not_ready` → `pending` и позже `skipped`;
    `not_configured`;
  - отмена (`pending` → `cancelled`, строки в аренде доходят);
  - инвариант счётчиков; удаление человека — «выбыли»; `finished_at`;
  - снятие `bot_blocked_at` при `by-telegram` и `config-link`;
  - значения по умолчанию из настроек в приглашении, `no_default_protocol`; `retention` не трогает счётчики.
- **test_migrations:** цикл upgrade/downgrade; CHECK `bot_settings.id`; строка `id = 1` есть после upgrade;
  частичные индексы; CASCADE от `vpn_users`.
- **integration:**
  - `test_bot_broadcast_flow`: ключ → рассылка на 1000 → `claim`/`ack` настоящим `PanelClient` через
    `ASGITransport` → итоги;
  - `test_bot_auth` (`outbox/*` в `BOT_ROUTES`, 401, лимит);
  - `test_bot_client_contract` (`claim`, `ack`);
  - `test_rbac`: новые маршруты в матрице, `bot_critical`, viewer не пишет;
  - аудит без текста и `telegram_id`; `Cache-Control: no-store` у `claim`; токена нет в `caplog`.
- **bot/tests:**
  - `FakeTelegram` получает режим лимитов: виртуальные часы; больше 30/с глобально или больше 1/с в чат → 429 с
    `retry_after`; заданные `chat_id` → 403 или 400;
  - `test_outbox`: 1000 сообщений на фальшивых часах (`sleep`/`clock` подставляются, как в `Watcher`) — ни одного
    429, в любом окне 1 с не больше `OUTBOX_RATE_PER_SECOND`, между сообщениями в один чат ≥ 1 с, полное время
    ≈ 50 с, все подтверждены;
  - 429 → общая пауза и повтор; 403 → `blocked`; 400 → `failed`; сеть → `retry`; аренда на исходе → `retry` без
    отправки;
  - журнал: рестарт до `ack` → досылка без повторной отправки `news`; `config` при новой аренде отправляется;
  - `parse_mode` HTML только у `news`; `test_log` — адреса и текста нет в выводе; `test_texts` (`broadcast_config`).
- **frontend (vitest):**
  - меню (ключей нет: виден только суперадмину);
  - `stale` → предупреждение;
  - ключ показан один раз и стирается при закрытии;
  - приглашение: ссылка и копирование, статусы;
  - «Повторить» у `failed`;
  - предпросмотр без `innerHTML` (`<img onerror>` выводится текстом), счётчик UTF-16, 422 с кодом → свой текст;
  - подтверждение с числом получателей; опрос прогресса останавливается по завершении; «Отменить»;
  - кнопка в карточке (нет Telegram — кнопки нет; заблокировал — неактивна; опрос до итога);
  - en; `i18n` сверка ключей; набор примеров HTML.

## Риски

- Двойная отправка (Telegram принял, `ack` потерялся, бот перезапущен без тома) → журнал бота, аренда с
  `lease_id`; для `config` повтор означает новую ссылку, прежняя гаснет — записать в компромиссы.
- Массовая рассылка конфигов гасит текущие ссылки получателей при выдаче → предупреждение в подтверждении;
  индивидуальная отправка не дублируется (частичный индекс).
- Старый бот и новая панель: очередь копится → предупреждение по `last_claim_at`; в INSTALL — обновлять бота вместе
  с панелью (удалённый — `git pull` и пересборка).
- Flood от Telegram из-за ответов в диалогах → запас 20 из 30, общая пауза на `retry_after`.
- Предпросмотр и Telegram разойдутся → общий набор примеров, строгий белый список, живая проверка всех тегов на
  тестовом аккаунте.
- XSS в предпросмотре → только React-элементы, ссылки не кликабельны, тест с `<img onerror>`.
- Секреты: токена нет в БД; `claim` с `no-store`; маска `/c#` в логах панели и бота; `telegram_id` получает любой
  действующий ключ — тот же компромисс, что в 25.1.
- Конкуренция: `SKIP LOCKED` на `claim`; выпуск ссылки — под блокировкой клиента (25.3); счётчики — одним `UPDATE`
  на рассылку.
- Большая постановка в запросе API: один `INSERT … SELECT`, предел 5000; дольше 1 с не бывает (проверить в живом
  прогоне).
- Downgrade теряет очередь, итоги и пометки «заблокировал» — `alembic/CLAUDE.md`.
- Живой прогон отправляет сообщения настоящим людям и выпускает ссылки — только с согласия пользователя и на
  тестовые аккаунты. dev- и prod-панель на одном сервере: dev-воркер не запускать (рассылке воркер не нужен).

## Проверка

- `uv run ruff check --no-cache . && uv run mypy src && uv run pytest` (один прогон за раз: общая тестовая БД).
- Из `bot/`: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`.
- Фронтенд: `npm run typecheck && npm run lint && npm test && npm run build`; после правок схем API —
  `export_openapi` с окружением CI и `npm run gen:api`.
- `uv run alembic upgrade head`, `downgrade -1`, снова `upgrade head`.
- Живой прогон (с согласия):
  - бот на сервере панели;
  - новость с каждым тегом себе и 2–3 тестовым аккаунтам;
  - один аккаунт блокирует бота → «заблокировали» и пометка в карточке, затем `/start` → пометка снята;
  - «Отправить ссылку в Telegram» → ссылка открывается, прежняя — 404;
  - массовая рассылка конфигов по протоколу на тестовых пользователей;
  - отмена посреди рассылки;
  - Lighthouse на `/bot/*`.
