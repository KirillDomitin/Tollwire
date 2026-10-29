# tests — бэкенд-тесты

`uv run pytest` (нужен `docker compose up`: Postgres + Redis). ~2100 тестов: полный прогон `-n 4` (xdist),
обычный — `--testmon` (только затронутые изменениями).

## Окружение (`conftest.py`)

- Переменные окружения задаются **до** импорта приложения: БД `vpn_panel_test` (пересоздаётся +
  `alembic upgrade head` на прогон), Redis db 15, свои `JWT_SECRET_KEY`/`ENCRYPTION_KEY`, `SCHEDULER_ENABLED=false`,
  `COOKIE_SECURE=false`, `PUBLIC_URL=https://panel.example.test` (адрес ссылок на страницу конфига). Предохранители: имя БД с `_test`, Redis не db 0 — рабочие данные не трогаются.
- После каждого теста — TRUNCATE таблиц + FLUSHDB.
- pytest-asyncio: `loop_scope="session"` и для тестов, и для фикстур (иначе ломается пул asyncpg).
- Общие фикстуры — `tests/fixtures.py` (подключены через `pytest_plugins`).
- xdist: у процесса `gwN` своя БД `vpn_panel_gwN_test` и Redis db `14-N` (`conftest.py`).
- Argon2 в тестах с дешёвыми параметрами (`security.ph` подменён в `conftest.py`): иначе ~0.25 с на каждый вход админа.
- `drain()` выходит сразу при пустой очереди (LLEN), без ожидания BRPOP; ждать «пустую очередь» в тесте —
  только явно через `process_next(timeout=...)`.

**Подробности — читать перед тестом в соответствующей области:** перечень тестов по слоям — `docs/tests/layers.md`;
фейк приложения AmneziaVPN (чтение, запись, сбои и гонки) — `docs/tests/fake-amnezia-app.md`; шлюз, выходы,
назначение, «Россия напрямую», failover (файлы тестов и возможности фейка шлюза) — `docs/tests/gateway.md`.

## Слои

| Папка | Что | Фикстуры |
|---|---|---|
| `unit/` | Чистые функции и провайдеры на `FakeServer`; `test_*_real_server.py` — настоящий AsyncSSH-сервер в процессе. | без БД |
| `services/` | Сервисы с настоящими Postgres и Redis; миграции (upgrade/downgrade) — `test_migrations.py`. | `session`, `fakes`, `world` (`tests/fixtures.py`) |
| `integration/` | Через HTTP (`httpx.ASGITransport`, `base_url=http://testserver`), настоящий вход, RBAC-матрица (`test_rbac.py`), сквозной сценарий `test_mvp_flow.py`, воркер через `process_next()` / `drain(worker)`. | `admin`, `api`, `make_admin`/`login_client`, `worker` (`integration/conftest.py`) |

Общие сцены: `tests/import_support.py` (сервер с приложением AmneziaVPN), `integration/discovery_support.py`,
`tests/gateway_support.py` (шлюз и выходы), `tests/geoip_support.py` (синтетические списки RIPE), бот: `tests/bot_support.py`
(сцены и обёртки), `tests/bot_client_support.py` (настоящий `PanelClient` без aiogram), `tests/bot_cli_support.py` (`Console`
скриптов ключей и приглашений); перечень тестов бота — `docs/bot/tests.md`.

## Установщик и `./panel`

Файлы: `unit/test_install_*.py` (в том числе бота: `test_install_bot.py`, `test_install_bot_remote.py`), `unit/test_panel.py`,
`unit/test_panel_bot.py`, `unit/test_bot_review_fixes.py`, `unit/test_provider_layout.py` (раскладка контейнера: подкласс с другим именем/папкой/подсетью не касается ничего «обычного», golden-журнал команд — `unit/golden/provider_default_commands.json`; `FakeServer(layout=...)`), `unit/test_ship_images.py`, `unit/test_letsencrypt.py`, `install_support.py`.

`install.sh` запускается по-настоящему (bash) в копии каталога во временной папке; Docker и curl — заглушки в `PATH`
(`FakeDocker` пишет вызовы в журнал, поведением управляют переменные `FAKE_*`; `FakeCurl` читает конфиг из `-K -` и
отвечает на getMe и heartbeat бота). Настоящий `.env.prod`, `certs/` и тома разработчика не затрагиваются. Язык
сообщений фиксирован (`PANEL_LANG=ru`). Тесты с правами POSIX и псевдотерминалом помечены `requires_posix`
(`install_support.py`): на Windows пропускаются, в CI идут на Linux. Ветка «создать админа в терминале» и вопрос
подтверждения `./panel restore` без псевдотерминала не проверяются — их смотреть живым прогоном на Linux (`script -qec`).
`./panel update` проверяется на настоящем git (локальный «сервер» + второй клон, из которого выходят версии).

## Бот

`bot/tests` — отдельный набор со своим окружением (`uv run --project bot pytest`), в корневом прогоне не участвует.
Здесь — то, что стыкует бот с панелью: `unit/test_bot_isolation.py` (AST-обход `bot/**/*.py`: бот не импортирует код
панели), `integration/test_bot_client_contract.py` (настоящий `PanelClient` против настоящего API: каждый метод,
каждый `reason` 409, 401, 429), `integration/test_bot_key_cli.py`, `integration/test_bot_invite_cli.py`.

Страница конфига: `unit/test_config_link_settings.py` (токен, адрес панели из `PUBLIC_URL`/`PANEL_DOMAIN`/`HTTPS_PORT`,
маскировка `token`), `services/test_config_links.py` (выпуск, открытие, отказы), `integration/test_public_config.py`
(HTTP, заголовки, одинаковый 404, лимиты по IP). Миграция `b4f1c8e27a69` — в `services/test_migrations.py`.

## Фейковый SSH (`fakes/fake_ssh.py`)

- `FakeServer` отвечает ровно на команды провайдера (контейнер, `awg0.conf`, `awg show`, замер метрик через
  `host_stats`); незнакомая команда → `AssertionError` — так тест ловит новые команды, о которых фейк не знает.
- Ключ панели в фейке: `home_dir`/`ensure_private_dir`/`read_file_optional` над `files`; вход по `private_key` —
  только если его blob есть в `/root/.ssh/authorized_keys` и `key_auth_enabled`; `on_key_command` — событие
  «посреди задачи» перед командой по ключу. Права и симлинки фейк не моделирует — они в
  `unit/test_authorized_keys_real_server.py` (POSIX-тесты пропускаются на Windows, гонять в Linux).
- Подмена — monkeypatch `ServerConnectionService.create_ssh_client`; в integration — autouse-фикстура
  `_no_real_ssh` (настоящий SSH в тестах невозможен).
- Новая SSH-команда в провайдере → научить ей `FakeServer`.
- Шлюз: `FakeServer(host=..., layout=GATEWAY_LAYOUT)`; на одном `FakeServer` — одна раскладка «своего» контейнера
  (обычный протокол и шлюз вместе не моделируются). Несколько серверов (шлюз + выходы) — несколько `FakeServer` в `world.fakes`.
- Сеть в тестах выключена: корневая autouse-фикстура `no_geoip_network` роняет скачивание списков RIPE; кому нужен
  ответ источника — `monkeypatch.setattr(geoip_service, "download_text", ...)`.

## Правила

- Гонки проверять детерминированно (окно через monkeypatch), а не через `asyncio.gather` по ASGI.
- Админ для интеграции — фикстура `admin` (superadmin), `make_admin`/`login_client` для других ролей.
- `test_rbac.py`: `/api/v1/bot/*` вне общей матрицы — проверяется метка `bot_key_required` на каждом маршруте и
  отсутствие прав админа; `/api/v1/bot-admin/*` — в матрице. `/api/v1/public/*` — отдельный список: ровно
  `POST /api/v1/public/config`, без прав и без ключа бота.
- Известный плавающий тест: `test_login_limited_by_ip` (окно лимита 60 с, Argon2 под нагрузкой).

## Маркер `slow`

Тесты, где каждый запускает bash-скрипт (`test_install_*`, `test_panel*`, `test_letsencrypt`) или гоняет миграции вниз-вверх (`test_migrations`); ~430 шт., 5–25 с на тест на Windows, помечаются в `conftest.py` (`pytest_collection_modifyitems`). Локально — `-m "not slow"` (~4 мин вместо ~12+); полный набор — при изменении `install.sh`, `panel`, `deploy/`, `alembic/` и перед слиянием. Postgres в `docker-compose.yml` запущен с `fsync=off` (только dev/тесты).

`run_alembic` (`conftest.py`) вызывает alembic в том же процессе, в потоке (`Config()` без `alembic.ini`, чтобы `env.py` не трогал логирование): подпроцесс стоил ~4 с на вызов, теперь тесты миграций вдвое быстрее. Нужна новая команда alembic — `getattr(command, имя)`.
