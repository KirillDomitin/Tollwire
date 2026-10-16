# tests — бэкенд-тесты

`uv run pytest` (нужен `docker compose up`: Postgres + Redis). ~223 теста.

## Окружение (`conftest.py`)

- Переменные окружения задаются **до** импорта приложения: БД `vpn_panel_test` (пересоздаётся +
  `alembic upgrade head` на прогон), Redis db 15, свои `JWT_SECRET_KEY`/`ENCRYPTION_KEY`, `SCHEDULER_ENABLED=false`,
  `COOKIE_SECURE=false`. Предохранители: имя БД с `_test`, Redis не db 0 — рабочие данные не трогаются.
- После каждого теста — TRUNCATE таблиц + FLUSHDB.
- pytest-asyncio: `loop_scope="session"` и для тестов, и для фикстур (иначе ломается пул asyncpg).
- Общие фикстуры — `tests/fixtures.py` (подключены через `pytest_plugins`).

## Слои

| Папка | Что |
|---|---|
| `unit/` | Чистые функции: awg_config, amnezia_link, host_metrics, host_keys, authorized_keys (текст файла), логи, правила, провайдер на фейке; `test_ssh_real_server.py` — настоящий AsyncSSH-сервер в процессе. |
| `services/` | Сервисы с настоящей БД и Redis: доступы, sync/health, ключ хоста, ключ панели, миграции (upgrade/downgrade), мониторинг. |
| `integration/` | Через HTTP (`httpx.ASGITransport`, `base_url=http://testserver` для cookie), настоящий login, RBAC-матрица по всем маршрутам, сценарий DoD целиком (`test_mvp_flow.py`), воркер через `process_next()`/`drain(worker)`, установка SSH-ключа панели. |

## Установщик и `./panel` (`unit/test_install_*.py`, `unit/test_panel.py`, `unit/test_ship_images.py`, `unit/test_letsencrypt.py`, `install_support.py`)

`install.sh` запускается по-настоящему (bash) в копии каталога во временной папке; Docker и curl — заглушки в `PATH`
(`FakeDocker` пишет вызовы в журнал, поведением управляют переменные `FAKE_*`). Настоящий `.env.prod`, `certs/` и
тома разработчика не затрагиваются. Язык сообщений фиксирован (`PANEL_LANG=ru`); проверки прав POSIX пропускаются на
Windows и идут в CI на Linux. Ветка «создать админа в терминале» и вопрос подтверждения `./panel restore` без псевдотерминала не
проверяются — их смотреть живым прогоном на Linux (`script -qec`). `./panel update` проверяется на настоящем
git (локальный «сервер» + второй клон, из которого выходят версии).

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

## Правила

- Гонки проверять детерминированно (окно через monkeypatch), а не через `asyncio.gather` по ASGI.
- Админ для интеграции — фикстура `admin` (superadmin), `make_admin`/`login_client` для других ролей.
- Известный плавающий тест: `test_login_limited_by_ip` (окно лимита 60 с, Argon2 под нагрузкой).
