# План: этап 25.3 — страница конфига

ТЗ: [telegram-bot.md](telegram-bot.md) (раздел 7, этап 25.3). Готовые этапы: [25.1](telegram-bot-25-1.md),
[25.2](telegram-bot-25-2.md).

## Задача

Человек получает от бота ссылку на личную страницу с конфигом (`vpn://`, QR, `.conf`, инструкции). Ссылка живёт,
пока жив доступ; токен в БД только хешем; новая ссылка (`/config`) гасит прежнюю.

## Не входит

Рассылка ссылок админом, экран «Бот», outbox, `admin-links`, ручной отзыв ссылки админом, показ ссылки в карточке
пользователя.

## Принятые решения

- **Р1-A.** Ссылка живёт в `modules/bot` (модель `config_links`). Действительность проверяется при каждом открытии
  (хуков в `access`, воркере и `sync` нет); `revoked_at` ставится при замене ссылки.
- **Р2-B.** Адрес ссылки `…/c#<токен>`, чтение — `POST /api/v1/public/config` с токеном в теле: фрагмент не уходит
  на сервер (нет в логах nginx и uvicorn, в Referer и у превью Telegram). Отступление от текста ТЗ (там `/c/<токен>` и
  GET).
- **Р3-A.** URL отдаёт только `POST /api/v1/bot/users/by-telegram/{id}/config-link` (токен виден один раз). Поле
  `config_url` убирается из `BotRegistrationRead` и `UserStatusRead`; бот вызывает `config-link` при `active` и на
  `/config`; в `/status` подсказка про `/config`.
- **Р4-A.** Публичный адрес панели — настройка `PUBLIC_URL`; если пусто, собирается из `PANEL_DOMAIN` и `HTTPS_PORT`
  (по умолчанию 443), как в `install.sh`. Валидатор: https (http только для localhost/127.0.0.1), без пути и query. Не
  задан — 409 `not_configured` и WARNING в лог.

## Модель

`config_links`: `id` UUID PK; `token_hash` VARCHAR(64) NOT NULL (`uq_config_links_token_hash`); `credential_id` UUID
NOT NULL, FK `vpn_credentials` ON DELETE CASCADE; `created_at`; `revoked_at`, `last_opened_at` NULL; частичный
уникальный индекс `uq_config_links_live_per_credential (credential_id) WHERE revoked_at IS NULL`. Одна ревизия
миграции после `a7d3e9b15c28`; downgrade удаляет индекс и таблицу (все ссылки теряются — люди снова пишут `/config`).

## Ссылка действительна, если одновременно

`link.revoked_at IS NULL`, `credential.revoked_at IS NULL`, `credential.source == ISSUED`,
`inactive_reason(grant, subscription, now) is None`. Одним запросом (link → credential → grant → subscription → user)
по `token_hash`; расшифровка — только после успешной проверки.

## Выбор credential для `/config` (config-link)

Кандидаты — живые credential пользователя с живым доступом. Есть ISSUED: credential гранта его бот-регистрации, если он
среди кандидатов, иначе самый новый. Только IMPORTED → 409 `config_unavailable`. Нет кандидатов, но на живом гранте идёт
`CREATE_USER` → 409 `not_ready`. Иначе → 409 `no_access`. Нет пользователя с таким `telegram_id` → 404. Выпуск:
`SELECT credential FOR UPDATE` → отзыв живой ссылки → вставка → commit; `IntegrityError` → один повтор.

## Публичный ответ

`POST /api/v1/public/config {token}` → `PublicConfigRead`: `name`, `valid_until`, `server_name`, `config` (.conf),
`share_link` (vpn://) или null, `share_qr` (список строк) или null, `file_name` (ASCII `[A-Za-z0-9_-]`, по умолчанию
`amnezia`). Форматы строит новый `VPNCredentialService.render_config(credential, grant)` (расшифровка и
`_app_formats` без аудита); `reveal_config` = аудит + `render_config`.

Безопасность: токен `generate_token(32)` (43 символа), формат проверяется до БД (`^[A-Za-z0-9_-]{43}$`); любой отказ
(неизвестный, кривой, заменённый, отозванный, истёкший, импортированный) — одинаковый 404 с постоянным телом
`{"detail":"Not found"}` без `Set-Cookie`; лимиты по IP: неудачи (`PUBLIC_CONFIG_IP_FAILURES` 20 за 600 с,
`ensure_not_exceeded` до поиска и `record` на отказе) и запросы (`PUBLIC_CONFIG_IP_REQUESTS` 60 за 60 с); заголовки
`Cache-Control: no-store`, `X-Robots-Tag: noindex, nofollow`; открытие пишет `last_opened_at`, в аудит не идёт;
создание ссылки — аудит `config_link_created` `{credential_id, user_id, source: "bot", replaced}` без токена и хеша.
Страховка маскировки: шаблон поля `token` в `core/log.py`.

## Фронтенд

Публичный маршрут `/c` вне авторизации и без каркаса панели (`AuthProvider` переносится с корня на маршруты входа и
защищённые, чтобы на публичной странице не было `POST /auth/refresh`); токен из `location.hash` уходит в теле POST;
собственный `createClient` с `credentials: "omit"`. Состояния: загрузка, данные, «ссылка недействительна или доступ
закончился — напишите боту /config» (одно сообщение на любой 404), 429, сеть. Порядок на странице: скопировать
`vpn://` и readonly-поле, скачать `.conf`, QR «с другого устройства» (серия, кнопка «Пауза/Дальше», подпись «Код N из M»
с `aria-live`), инструкции `<details>` по Android/iOS/Windows/macOS, предупреждение «не пересылайте; новая ссылка —
/config». `QrSeries`/`QrCode` выносятся из `ConfigDialog` в общий компонент. Данные не кэшируются (`gcTime: 0`) и
стираются на `pagehide`. Только CSS-модули (CSP: `style-src 'self'`), токены дизайна, `lang`/`title`/`main`/один `h1`.
Словарь `public.json` ru/en; `AUDIT_ACTIONS` += `config_link_created` (группа `bot`). Цель: Lighthouse
«Доступность» 100 (mobile и desktop, обе темы).

## nginx

`X-Robots-Tag: noindex, nofollow` одной строкой на уровне server (новый `location` с `add_header` отменил бы заголовки
server). Фрагмент в логи не попадает, маска пути не нужна.

## Бот

`PanelClient.config_link(telegram_id)` → `{config_url, valid_until}`; ошибки 409 с `reason` (`config_unavailable`,
`not_ready`, `no_access`, `not_configured`) и 404. Watcher на `active` вызывает `config_link`: `not_ready` и `PanelError`
→ следующий опрос; `config_unavailable`/`no_access` — свой текст. Команда `/config` (новая ссылка, старая гаснет),
`/help`, меню команд, `already_registered` с `/config`. Текст `ready_link` со сроком и предупреждением; `ready_stub`
удаляется. `DefaultBotProperties(link_preview_is_disabled=True)`. `log.py`: URL со ссылкой на конфиг не пишется.

## Шаги

- [ ] 1. `core`: `config.py` (`public_url`, адрес из `panel_domain`/`https_port`, валидатор, лимиты `PUBLIC_CONFIG_*`;
  `.env.example`, `.env.prod.example`, комментарий про `PANEL_DOMAIN`), `exceptions.py` (`ReasonConflictError`,
  `RegistrationConflictError` — потомок; `PublicUrlNotConfiguredError`), `main.py` (`reason` для базового класса),
  `log.py` (шаблон `token`).
- [x] 2. Модель `config_links`, миграция, импорт в `alembic/env.py`.
- [x] 3. `access`: `VPNCredentialService.render_config`; репозиторий — живые credential пользователя с грантом и подпиской.
- [x] 4. `modules/bot`: `ConfigLinkRepository`, `config_links.py` (`ConfigLinkService.issue_for_telegram`, `open`), лимиты,
  аудит `CONFIG_LINK_CREATED`/`AuditEntity.CONFIG_LINK`.
- [x] 5. Роутеры: `POST …/config-link` в Bot API, новый `api/v1/public.py` (`POST /api/v1/public/config`), схемы
  (`config_url` убрать из `BotRegistrationRead`/`UserStatusRead`), `include_router`.
- [x] 6. nginx: `X-Robots-Tag`.
- [x] 7. Фронтенд: `export_openapi` → `gen:api`; `AuthProvider` на маршруты; `api/public.ts`; `components/QrSeries.tsx`;
  `pages/public/PublicConfigPage`; словари; `AUDIT_ACTIONS`.
- [x] 8. Бот: `panel.py`, `watcher.py`, `commands.py`, `texts.py`, `__main__.py`, `log.py`.
- [ ] 9. Документация: `modules/bot/CLAUDE.md`, `docs/bot/api.md`, `docs/bot/config-page.md`, `docs/bot/tests.md`,
  `access/CLAUDE.md`, `core/CLAUDE.md`, `alembic/CLAUDE.md`, `frontend/CLAUDE.md`, `docs/frontend/structure.md`,
  `bot/CLAUDE.md`, `docs/bot-service/*`, `tests/CLAUDE.md`, INSTALL (`PUBLIC_URL`), CHANGELOG. Issues `tech-debt`:
  `DISABLED` не гасит доступ; чистка отозванных ссылок.

## Тесты

- **unit:** формат и длина токена; адрес из `PUBLIC_URL`/`PANEL_DOMAIN`/`HTTPS_PORT`, отказ для http не-localhost;
  маскировка `token`.
- **services/test_config_links.py:** в БД только хеш; второй выпуск отзывает первый, живая одна; гонка (окно monkeypatch);
  выбор credential; 409 `config_unavailable`/`not_ready`/`no_access` без строк и аудита; открытие: формы конфига,
  `last_opened_at`, аудита нет; 404 при замене ссылки, отозванном credential, отозванном гранте до задачи
  `REVOKE_ACCESS`, прошедшем сроке без планировщика, отозванной подписке, каскадном удалении; в аудите нет токена и хеша.
- **test_migrations:** цикл upgrade/downgrade, частичный индекс, CASCADE.
- **integration/test_public_config.py:** заголовки, нет `Set-Cookie`; тела 404 побайтно одинаковы; 429 с `Retry-After`
  после N неудач и открытие с другого IP; токена нет в `caplog` за весь запрос.
- **integration, бот:** `test_bot_flow` (регистрация → active → config-link → открытие → отзыв доступа → 404 → `/config`
  → 409 `no_access`); `test_bot_auth` (новый маршрут в `BOT_ROUTES`); `test_bot_client_contract` (`config_link`);
  `test_rbac` (исключить `/api/v1/public/`, публичные маршруты — ровно ожидаемый список, без прав и ключа бота).
- **bot/tests:** FakePanel `config-link`; watcher (active → ссылка, `not_ready` → повтор, сбой отправки → повтор с новой
  ссылкой); `/config` для каждого `reason` и 404; `test_texts`; `test_log`; `link_preview` выключен в `SendMessage`.
- **frontend (vitest + Testing Library):** `PublicConfigPage.test.tsx` (токен из hash уходит в теле, не в URL; без
  `Authorization`; без вызова `/auth/refresh`; имя и дата; копирование; скачивание; 404 — одно нейтральное сообщение
  без `detail`; 429; en; пауза серии), маршрут `/c` не уводит на `/login`, `i18n` сверка ключей, регресс `ConfigDialog`.

## Риски

- Ссылку переслали: предупреждение в тексте; `/config` гасит старую; отзыв доступа гасит сразу.
- Старый бот и новая панель: выпускать вместе; бот терпим к отсутствию `config_url`.
- Неверный `PUBLIC_URL`: валидатор, 409 `not_configured`, проверка в живом прогоне, раздел в INSTALL.
- CGNAT и лимит: считаются неудачи, а не открытия. Redis недоступен: лимиты пропускают (fail-open), остаётся nginx.
- bfcache и общий компьютер: `no-store` и очистка на `pagehide`. Встроенный браузер Telegram без clipboard и скачивания:
  readonly-поле и подсказка «откройте в браузере».
- Downgrade миграции теряет только ссылки.
- Живой прогон выдаёт клиента на реальном сервере — только с согласия пользователя (dev- и prod-панель на одном сервере).
- Рассылка в следующем этапе: ссылку для рассылки выпускать в момент выдачи сообщения боту, а не при постановке в
  очередь (иначе токен лежал бы в БД открытым текстом).
