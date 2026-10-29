# Справочник (вынесено из CLAUDE.md)

Описательные части: карта файлов, модели, API, операции. Инварианты и грабли остались в CLAUDE.md.

## Файлы

| Путь | Что |
|---|---|
| `src/tollwire_bot/config.py` | `Settings`: `TELEGRAM_BOT_TOKEN`, `BOT_API_KEY` (SecretStr), `PANEL_URL`, `PANEL_CA_FILE`, `STATE_DIR` (`/data`), `LOG_LEVEL` |
| `panel.py` | `PanelClient` — единственный путь бота к панели (httpx и stdlib), ретраи, коды ответов → исключения; `config_link` → `ConfigLink` (адрес в `repr` не показывается) |
| `texts.py` | Все тексты: `TEXTS[key]["ru"]`, `t(key, lang, **kw)`, `format_date`. `parse_mode` не задан |
| `validation.py` | Имя (1–64 после strip, без Cc/Cf/Cs/Zl/Zp), почта (regex, до 100), код `start` (`[A-Za-z0-9_-]{1,64}`) |
| `storage.py` | `SqliteStorage(BaseStorage)`: состояние диалога и список регистраций в одном файле |
| `dialog.py` | Регистрация: `/start <код>`, состояния `name → email → confirm → submitting`, кнопки «Да» / «Изменить» |
| `commands.py` | `/status`, `/config`, `/help`; всё прочее — ответ по умолчанию (регистрируется последним) |
| `refusals.py` | `CONFIG_REFUSAL_TEXT`: причина 409 `config-link` → ключ текста (`config_unavailable`, `config_no_access`, `config_not_ready`, `config_not_configured`) |
| `watcher.py` | `Watcher`: опрос `GET /registrations/{id}` и итог человеку (при `active` — ссылка из `config-link`); список ожидающих лежит в хранилище |
| `heartbeat.py` | `getMe` при старте, `POST /heartbeat` каждые 30 с; лог только при смене состояния |
| `log.py` | `RedactingFormatter` (адрес страницы конфига `…/c#…` маскируется целиком), `configure_logging` (один обработчик на корне) |
| `app.py` | `build_dispatcher(settings, panel, storage, *, sleep, clock)`, `UserThrottle`, фильтр личных чатов |
| `__main__.py` | Сборка и запуск: `getMe`, меню команд (`status`, `config`, `help`), превью ссылок выключено (`DefaultBotProperties(link_preview_is_disabled=True)`), фоновые задачи, `/tmp/alive`, очистка по часу, SIGTERM |
| `__init__.py` | Только `__version__` |
| `Dockerfile`, `docker-compose.yml`, `.env.example`, `install-bot.sh`, `pyproject.toml`, `uv.lock` | Образ, вариант «другой сервер», установщик, зависимости |

## Установка

- На сервере панели: `deploy/bot.sh` (общий код `install.sh` и `./panel bot`), сервис `bot` в `docker-compose.prod.yml`
  (профиль `bot`, `read_only`, `cap_drop: ALL`, сеть `edge`, том `bot_data`).
- На другом сервере: `bot/install-bot.sh`. Пишет `bot/.env`, а если сертификат панели самоподписанный — и
  `bot/certs/panel-ca.pem`, только после сверки отпечатка. Запускает `bot/docker-compose.yml`.

## Healthcheck

Бот раз в 10 с touch-ит `/tmp/alive`, пока последний ответ панели на heartbeat не старше 90 с. Compose проверяет, что
файл не старше 90 с.

## Тесты

- `bot/tests`: `FakeTelegram` (перехват запросов к Bot API) и `FakePanel` (`httpx.MockTransport`). Без сети и без
  панели. Из корня: `uv run --project bot pytest`.
- Проверки как в CI (задача `bot`), из `bot/`: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`.
- Тесты панели, которые касаются бота (изоляция, контракт клиента, CLI, установщики), — в корневом `tests/`, см.
  `docs/bot/tests.md`.

## Не входит

Нет рассылок и очереди сообщений, уведомлений администратору,
сообщения администратору о недоступной панели, установки по SSH и экрана в панели.
