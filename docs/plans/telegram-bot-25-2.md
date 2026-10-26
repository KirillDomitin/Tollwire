# План: этап 25.2 — бот и установка (на сервере панели и на отдельном сервере)

ТЗ: [telegram-bot.md](telegram-bot.md) (раздел 12, этап 25.2). Готовый контракт Bot API: `docs/bot/api.md`,
`docs/bot/registration.md`, `src/modules/bot/CLAUDE.md`.

## Задача

Каталог `bot/` (aiogram 3.31, long polling): диалог регистрации (ТЗ 6.1), `/status`, `/help`, heartbeat; бот ходит
только в Bot API панели и не импортирует `src/`. Установка: `install.sh` (бот на этом сервере / на другом / не
нужен), `bot/install-bot.sh`, `./panel bot enable|disable|key|status|invite`. Ключ бота и приглашения выпускаются
консольными скриптами панели. В панели миграций, маршрутов и прав нет.

## Не входит

`/config` и `config-link` (25.3), outbox/рассылки/`bot_blocked_at` (25.4), `admin-links`, уведомления админу,
«панель недоступна» (25.5), установка бота по SSH (25.7). Пока `config_url=null`, при `active` бот пишет «Доступ
готов до ДД.ММ.ГГГГ. Конфиг пришлёт администратор; срок доступа — /status» (код: `if config_url: ссылка else
заглушка`). Подсказку `/config` не показываем. Бот отвечает только в личных чатах; `parse_mode` не задан (имя и
почта пользователя в тексте подтверждения — иначе инъекция HTML).

## Принятые решения

- **Р1-A.** Тесты бота — `bot/tests` со своим окружением (`uv run --project bot pytest`); в корневом `tests/` —
  изоляция, контракт клиента, CLI ключа/приглашений, установщик.
- **Р2-B.** FSM-хранилище — своё `SqliteStorage(BaseStorage)` (stdlib `sqlite3` через `to_thread`) в томе `/data`
  (файл 0600), срок жизни записи 24 ч, чистка при старте; там же список активных регистраций для watcher (опрос
  продолжается после рестарта).
- **Р3-B.** Секреты бота на сервере панели — отдельный `.env.bot` (0600, только у сервиса `bot`:
  `env_file: {path: .env.bot, required: false}`, Compose ≥ 2.24); в `.env.prod` — одна строка `COMPOSE_PROFILES=bot`
  (иначе напечатанные `down`/`up -d` не тронут бота). Отличие от ТЗ §10 (переменные бота в `.env.prod`) осознанное:
  токен Telegram не должен попадать в окружение api/worker/migrate (ТЗ §2/§11).
- **Р4-A.** Ключ бота — CLI `src/scripts/bot_key.py` через `compose run --rm -T api python -m scripts.bot_key …`
  (`BotKeyService`, аудит `source: cli`).
- **Р5-A.** Без `config_url` — заглушка «конфиг пришлёт администратор».
- **Р6-B.** Приглашения без UI — CLI `src/scripts/bot_invite.py` + `./panel bot invite` (печатает ссылку t.me).
- **Р7.** Новая задача `bot` в `.github/workflows/ci.yml` (`uv sync --frozen` в `bot/`, ruff, mypy, pytest).

## Структура bot/

```
bot/pyproject.toml (tollwire-bot; aiogram>=3.31,<3.32; httpx>=0.28.1,<0.29; pydantic-settings; dev: pytest,
  pytest-asyncio, ruff, mypy), uv.lock, Dockerfile (тот же python-образ, что корневой; uid 999; /data), .dockerignore,
  .env.example, docker-compose.yml (вариант «другой сервер»: read_only, cap_drop ALL, no-new-privileges, том
  bot_data:/data, ./certs:/certs:ro, healthcheck), install-bot.sh, CLAUDE.md
bot/src/tollwire_bot/: __init__ (только __version__), __main__ (сборка/запуск, SIGTERM → stop_polling, закрытие
  клиента и хранилища), config (TELEGRAM_BOT_TOKEN, BOT_API_KEY — SecretStr, PANEL_URL, PANEL_CA_FILE, STATE_DIR,
  LOG_LEVEL), panel (PanelClient — только httpx + stdlib), texts (TEXTS[key]["ru"], t(key, lang="ru", **kw)),
  validation (имя 1–64 после strip, без Cc/Cf/Cs/Zl/Zp; почта — простой regex; код ^[A-Za-z0-9_-]{1,64}$),
  storage, dialog (Router: /start <код> и без кода, состояния name → email → confirm → submitting, callback
  «Да/Изменить»), commands (/status, /help, прочее), watcher, heartbeat, log, app (build_dispatcher(settings,
  panel, storage) — точка входа для тестов)
bot/tests/
```

### PanelClient

- Адрес `https://`; `http://` только для внутренних имён без точки (`api`, сеть compose).
- `follow_redirects=False`; заголовки `X-Bot-Key`, `User-Agent: tollwire-bot/<версия>`.
- TLS: `ssl.create_default_context(cafile=certifi.where())` + `load_verify_locations(PANEL_CA_FILE)`, если задан
  (в дополнение к штатному хранилищу, не вместо).
- Таймауты `httpx.Timeout(10, connect=5)`. Ретраи свои: сеть, таймаут, 502/503/504 → бэкофф 0.5/1/2/4 с, затем
  `PanelUnavailable` (безопасно для всех методов: повтор `POST /registrations` идемпотентен).
- Коды: 401 → `PanelAuthError`, без ретрая, ERROR в лог один раз; 404 → `None`; 409 → `Conflict(reason)` (неизвестный
  reason = `conflict`); 422 → `Rejected(field)` по `loc[-1]` (`detail` не логировать — там ввод); 429 → подождать
  `Retry-After`, если ≤ 10 с, и повторить один раз, иначе `RateLimited`.

### Диалог

1. Невалидный код — «ссылка недействительна».
2. `GET users/by-telegram`: 200 → «вы уже зарегистрированы, /status».
3. `invites/check`: `valid` → спросить имя; `used`/`expired`/`revoked`/`unknown` — свои тексты.
4. «Да»: замок на пользователя (`asyncio.Lock`), состояние `submitting`, кнопки снимаются, затем `POST`.
5. Ответы на POST: 201 → «Готовим доступ…» и в watcher; `email_taken` → снова шаг почты (имя и код сохраняются);
   `telegram_taken` → «уже зарегистрированы»; `invite_*` → конец диалога; `PanelUnavailable` → остаёмся в
   `confirm`, можно нажать «Да» ещё раз.
6. Watcher: `GET /registrations/{id}` через 2, 3, 5 с, затем каждые 10 с, до 3 мин (~25 запросов). Итог: `active`,
   `failed` («не получилось, мы разберёмся»), таймаут («ещё готовится, проверьте /status»).
7. `/status`: имя, `valid_until`, протокол; 404 — «вы не зарегистрированы».
8. Heartbeat: `getMe` один раз при старте, затем `POST /heartbeat` каждые 30 с; сбои логируются только при смене
   состояния (ok ↔ down).

### Логи бота

Фильтр на корневом обработчике: точные значения токена и ключа + шаблоны `\d{6,}:[A-Za-z0-9_-]{30,}` и
`twb_[A-Za-z0-9_-]+`; логгер `httpx` — WARNING; не логируются тексты сообщений, данные FSM, тела запросов и ответов,
`detail` ответа 422; исключения `PanelClient` несут только код и `reason`.

## Выпуск ключа и установка

- `src/scripts/bot_key.py` по образцу `create_admin` (Console, `run(argv, console)`, MESSAGES ru/en):
  `create --name N [--raw]` (в терминале — рамка «показан один раз»; не в терминал — только с `--raw`, иначе отказ),
  `revoke` (ключ из stdin), `list`, `seen --within S` (ключ из stdin, exit 0, если heartbeat не старше S секунд).
  В `BotKeyService.create` — необязательная мета `{"source": "cli"}`.
- `src/scripts/bot_invite.py`: `protocols`, `create --protocol --days --expires-in --note` → ссылка t.me; аудит
  `source: cli`; `BotInviteService.create` — та же необязательная мета.
- `install.sh`, этот сервер: токен скрыто (`read -s`) или `TELEGRAM_BOT_TOKEN`; `getMe` через `curl -K -` (URL с
  токеном в stdin); ключ `bot_key create --raw`; запись `.env.bot` (umask 077, tmp + mv); `COMPOSE_PROFILES=bot`;
  `compose up -d [--build] bot`; ожидание `seen --within 90` (нет heartbeat — подсказка `./panel logs bot`, установка
  не падает). Ключ на экран не выводится никогда. `getMe` не прошёл → предупреждение и выбор: другой сервер / всё
  равно / отмена.
- `install.sh`, другой сервер: ключ выпускается; не в терминал — отказ с подсказкой `./panel bot key --remote`;
  печатается `git clone <https-URL> tollwire && cd tollwire && ./bot/install-bot.sh --panel-url https://… --panel-fingerprint <SHA-256>`;
  ключ отдельной строкой один раз и в команду не вставляется (install-bot.sh спросит скрыто). URL репозитория —
  `git remote get-url origin` (`git@github.com:` → `https://`, userinfo вырезается; переопределение
  `TOLLWIRE_REPO_URL`).
- Вопрос о боте — только при свежей установке (`env_created=1`) в терминале либо с флагом `--bot local|remote|none`;
  повторные запуски и `./panel update` не спрашивают; при `--no-start` шаг пропускается с подсказкой.
- `./panel bot`: `enable` (диалог как в install.sh, если нет `.env.bot`; профиль; `up -d bot`), `disable` (`stop` +
  `rm -f bot`, убрать из `COMPOSE_PROFILES`; ключ остаётся действующим — об этом в выводе), `key` (новый ключ в
  `.env.bot`, `up -d --force-recreate bot`, проверка `seen`, затем отзыв старого через stdin), `key --remote`
  (показать один раз; `--revoke PREFIX`), `status` (`bot_key list`), `invite` (`bot_invite`).
- `deploy/bot.sh` (общий код, `lib_msg_ru`/`lib_msg_en`) подключается из install.sh и panel; `env_set` в
  `deploy/common.sh` (атомарная замена одной строки, 0600); в `panel` к `SERVICES` добавляется `bot`;
  `deploy/ship-images.sh` — третий образ, если есть; отпечаток в `./panel cert status`.
- `bot/install-bot.sh`: подключает `../deploy/common.sh`; проверяет Docker и Compose ≥ 2.24; спрашивает адрес панели
  (только https), ключ и токен (скрыто или из `BOT_API_KEY`/env); `getMe`; heartbeat (`version=install`); ошибка TLS
  (curl exit 60) → `openssl s_client -showcerts` → сверка отпечатка с `--panel-fingerprint` или подтверждение →
  `bot/certs/panel-ca.pem` и `--cacert`; 401 → «ключ неверный или отозван»; запись `bot/.env` (0600),
  `docker compose -f bot/docker-compose.yml up -d --build`, ожидание `healthy`; `--reconfigure` — только сменить ключ.
- `docker-compose.prod.yml`: сервис `bot` (`profiles: [bot]`, `build: ./bot`, `image: vpn-panel-bot:latest`,
  `env_file .env.bot (required: false)`, `PANEL_URL=http://api:8000`, сеть `edge`, том `bot_data`, hardened).

## Тесты

- **bot/tests:** `FakeTelegram` (перехват методов через `BaseRequestMiddleware` сессии, журнал SendMessage /
  EditMessageReplyMarkup / AnswerCallbackQuery; апдейты через `dp.feed_raw_update(bot, update)`, `Bot("42:TEST")`);
  `FakePanel` (`httpx.MockTransport` с состоянием по `docs/bot/api.md`; режимы 401/409/422/429/503/обрыв);
  `test_dialog` (весь 6.1, каждый reason, двойное «Да» → один POST, 422 по имени и почте, панель упала на «Да»),
  `test_watcher` (active/failed/таймаут, продолжение после «рестарта» на том же хранилище), `test_panel_client`
  (ретраи, без редиректов, Retry-After, PANEL_CA_FILE, настоящий TLS-сервер с сертификатом той же openssl-командой,
  что в install.sh), `test_storage`, `test_heartbeat`, `test_texts` (каждый ключ есть, подстановки сходятся),
  `test_log` (токен, ключ, почта не попадают в вывод за сквозной диалог).
- **Корневой tests/:** `unit/test_bot_isolation.py` (AST-обход `bot/**/*.py`: нет импортов `core`, `modules`,
  `infrastructure`, `scripts`, `main`, `worker`, `src`); `integration/test_bot_client_contract.py` (настоящий
  `PanelClient` против настоящего API через `ASGITransport`: все методы, 409-reason, 401, 429);
  `integration/test_bot_key_cli.py`, `test_bot_invite_cli.py` (по образцу `test_create_admin_cli.py`: хеш в БД, аудит
  `source: cli` без ключа и кода, `--raw` вне терминала, отказ без `--raw`, `revoke`/`seen` через stdin);
  установщик: `unit/test_install_bot.py`, `test_panel_bot.py`, `test_install_bot_remote.py` (запуск по-настоящему в
  копии каталога; заглушки `FakeDocker` — `compose run … scripts.bot_key create`, `seen`/`revoke`, `up/stop/rm bot`,
  `ps -q bot`; `FakeCurl` читает `-K -` и отвечает getMe и heartbeat). Проверяется: `.env.bot` 0600 (POSIX),
  `COMPOSE_PROFILES=bot` и остальные строки `.env.prod` байт в байт, ключ и токен не встречаются ни в аргументах
  `docker.log`, ни в stdout/stderr, «другой сервер» без терминала отказывается показывать ключ, `getMe` не прошёл →
  предупреждение, повторный install.sh и `update` не спрашивают, `disable`/`enable`/`key` (старый ключ отзывается
  после записи нового), сообщения на обоих языках, install-bot.sh: https-only, 401, отпечаток не совпал → отказ без
  записи `bot/.env`.

## Шаги

- [x] 1. `bot/`: pyproject, uv.lock, каркас пакета, `config`, `log`, `texts`, `validation`.
- [x] 2. `panel.py` (клиент и ошибки) + `bot/tests/test_panel_client.py`; корневой `test_bot_client_contract.py`.
- [x] 3. `storage`, `dialog`, `commands`, `watcher`, `heartbeat`, `app`, `__main__` + тесты диалога, watcher, логов.
- [x] 4. `tests/unit/test_bot_isolation.py`.
- [x] 5. `bot/Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`, healthcheck (`/tmp/alive`: опрос Telegram
  и последний ответ панели не старше 90 с); корневой `.dockerignore` += `bot`.
- [x] 6. Панель: `src/scripts/bot_key.py`, `src/scripts/bot_invite.py`, необязательная мета в `BotKeyService.create` /
  `BotInviteService.create`; тесты CLI.
- [x] 7. `docker-compose.prod.yml`: сервис `bot` с профилем.
- [ ] 8. Установка: `deploy/common.sh` (`env_set`), `deploy/bot.sh`, `install.sh` (флаг `--bot`, шаг после админа,
  проверка `vpn-panel-bot:latest` при `--no-build` с профилем), `panel` (`cmd_bot`, `SERVICES`, отпечаток в
  `cert status`), `deploy/ship-images.sh`, `bot/install-bot.sh`.
- [ ] 9. Тесты установщика; `tests/unit/install_support.py` (`deploy/bot.sh`, `bot/install-bot.sh`, новые заглушки).
- [ ] 10. CI-задача `bot`.
- [ ] 11. Документация: `INSTALL.md`/`INSTALL.ru.md` (раздел «Telegram-бот»), `bot/CLAUDE.md`, `src/modules/bot/CLAUDE.md`,
  `src/scripts/CLAUDE.md`, `tests/CLAUDE.md`, `docs/bot/tests.md`, корневой `CLAUDE.md`, `CHANGELOG*.md`.

## Риски

- Самоподписанный сертификат панели может не пройти строгую проверку X.509 в Python 3.14 → тест с сертификатом от
  той же openssl-команды; при провале — `basicConstraints=critical,CA:TRUE` и `keyUsage` в `install.sh` (только для
  новых сертификатов). Сертификат обновили → удалённый бот перестал доверять: отпечаток в `./panel cert status`,
  `install-bot.sh --reconfigure-ca`.
- `docker compose down` без профиля оставляет бота → `COMPOSE_PROFILES` в `.env.prod`; проверка
  `docker compose --env-file .env.prod -f docker-compose.prod.yml config --services` с профилем и без; если env-файл
  профиль не подхватывает — скрипты добавляют `--profile bot` сами. Compose < 2.24 не понимает `required: false` →
  проверка версии.
- Telegram недоступен с сервера панели → `getMe` до выпуска ключа, предложение «другой сервер».
- Токен/ключ в `ps`, логах, истории shell → `curl -K -`, stdin для `revoke`/`seen`, `read -s`, ключ не в
  напечатанной команде, тест «секрета нет в журнале docker и в выводе».
- Бот в сети `edge`, api доверяет `X-Forwarded-For` из неё (`docker-compose.prod.yml`) → взломанный бот подделает IP
  в логах и лимитах по IP; лимит по ключу от IP не зависит. Принято, записать в `bot/CLAUDE.md`.
- Имя и почта на диске (Р2) → файл 0600 в томе, удаление по завершении, срок жизни 24 ч.
- Живой прогон выдаёт клиента на реальном сервере — только с согласия пользователя; dev- и prod-панель на одном
  сервере (`sync` удаляет чужие `grant-*`).
- Heartbeat требует `bot_username` из `getMe` → проверка панели в `install-bot.sh` возможна только после `getMe`.
