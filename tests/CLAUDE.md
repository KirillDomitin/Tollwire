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
| `unit/` | Чистые функции: awg_config (в т.ч. `test_awg_discovery.py` — clientsTable и peer'ы чужой установки), amnezia_link, host_metrics, host_keys, authorized_keys (текст файла), логи, правила, провайдеры на фейке (`test_provider.py`; `test_amnezia_app.py` — только чтение приложения AmneziaVPN, `test_amnezia_app_write.py` — запись (отзыв,
`restore_peer`, протокол записи), `test_import_read.py` — `read_importable` для импорта (только чтение, PSK не утекает), `test_fake_amnezia_app.py` и `test_fake_amnezia_app_write.py` — страховка фейка); `test_ssh_real_server.py` — настоящий AsyncSSH-сервер в процессе. |
| `services/` | Сервисы с настоящей БД и Redis: доступы, sync/health (в том числе возврат peer'ов импортированных клиентов — `test_sync_imported.py`), ключ хоста, ключ панели, миграции (upgrade/downgrade, в том числе миграция импорта 23.4 с данными нового вида), мониторинг. |
| `integration/` | Через HTTP (`httpx.ASGITransport`, `base_url=http://testserver` для cookie), настоящий login, RBAC-матрица по всем маршрутам, сценарий DoD целиком (`test_mvp_flow.py`), воркер через `process_next()`/`drain(worker)`, установка SSH-ключа панели, поиск Amnezia (`test_discovery_job.py` — задача и автопоиск после установки, `test_discovery_api.py` — API и статусы клиентов; общее — `discovery_support.py`), отзыв клиента приложения AmneziaVPN (`test_app_revoke.py`: строки протокола и credential
в БД создаются вручную). Импорт клиентов приложения в БД панели (этап 23.4): `test_import_api.py` (POST /servers/{id}/import: права,
422/404/409, params задачи), `test_import_job.py` (воркер: пользователь/подписка/грант/credential, повтор, gone/unsupported/
already/conflict, savepoint на клиента, аудит, секреты), `test_import_lifecycle.py` (отзыв и истечение импортированного
клиента, 409 на конфиг, `source`/`external_name` в API, пользователи без email, `DELETE /protocol` не трогает приложение);
общее — `tests/import_support.py` (сцена «сервер с приложением», импортированный credential в БД). |

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
- Приложение AmneziaVPN (контейнеры `amnezia-awg` с `wg`/`wg0` и `amnezia-awg2` с `awg`/`awg0`, конфиг и `clientsTable`
  внутри контейнера, монтирование не из папки панели): `add_amnezia_app(container, peers, clients_table, running, port)`
  возвращает `AmneziaApp` (`secrets` — серверный приватный ключ и PSK для проверок «не утекло», `files`, `interface_up`).
  Чтение внутри контейнера — только `docker exec <к> cat /opt/amnezia/awg/<файл>` и `<wg|awg> show <интерфейс>
  latest-handshakes`; чужая утилита → код 126, остановленный контейнер → ошибка Docker, прочее → `AssertionError`.
  `changing_commands()` — как `mutating_commands()`, но эти два чтения изменением не считаются (для провайдеров
  чужого контейнера; `mutating_commands()` остаётся строгим — любой docker exec там изменение). `container_reads` —
  журнал прочитанных внутри контейнеров файлов (проверка «секретные файлы не читали»).
- Запись в приложение (этап 23.3, `test_amnezia_app_write.py`, `integration/test_app_revoke.py`): фейк понимает
  `docker cp <STAGE_DIR>/<контейнер>.conf|.clientsTable <контейнер>:<конфиг|clientsTable>` (иные пути и чужие
  контейнеры, в том числе контейнер панели, — `AssertionError`), `rm -f <файл из STAGE_DIR>` и
  `docker exec <к> bash -c '<утилита> syncconf <iface> <(<утилита>-quick strip <конфиг>)'` (разбирает конфиг и
  обновляет «живых» `app.peers`; мусорный конфиг — код 1). `stage_files()` — промежуточные файлы на хосте (после
  любой операции их быть не должно), `unexpected_changing_commands()` — изменяющие команды сверх протокола записи,
  `copies` (что и с какими правами скопировали), `file_modes` (SFTP всегда 0600), `write_attempts`. Сбои и гонки:
  `app.apply_fails`, `app.docker_cp_fails` (пути в контейнере), `fake.write_fails` (пути SFTP на хосте),
  `app.on_read(путь, номер_чтения)` — подмена файла «приложением» между чтением и записью, `app.on_apply` — действие
  перед syncconf (например, сломать откат). `mutating_commands()` остаётся строгим. Дополнительно: `mkdir -p
  /opt/vpn-admin` и `ensure_private_dir` (фейк запоминает 0700 в `dirs`) — как настоящие; `rm -f` вне `STAGE_DIR` —
  `AssertionError`; `app.read_counts` (сколько раз прочитан путь), `app.applies` (сколько раз syncconf применил
  конфиг). Сбой `strip` фейк не моделирует (`<утилита>-quick strip` в фейке всегда успешен), как и пустой конфиг,
  сносящий peer'ов: это проверено только на живом контейнере (см. «Грабли» в `src/infrastructure/vpn/CLAUDE.md`).

## Правила

- Гонки проверять детерминированно (окно через monkeypatch), а не через `asyncio.gather` по ASGI.
- Админ для интеграции — фикстура `admin` (superadmin), `make_admin`/`login_client` для других ролей.
- Известный плавающий тест: `test_login_limited_by_ip` (окно лимита 60 с, Argon2 под нагрузкой).
