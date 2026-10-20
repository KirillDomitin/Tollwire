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

## Установщик и `./panel` (`unit/test_install_*.py`, `unit/test_panel.py`, `unit/test_provider_layout.py` (раскладка контейнера: подкласс с другим именем/папкой/подсетью не касается ничего «обычного», golden-журнал команд — `unit/golden/provider_default_commands.json`; `FakeServer(layout=...)`), `unit/test_ship_images.py`, `unit/test_letsencrypt.py`, `install_support.py`)

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

## Шлюз (этап 24.1)

- `unit/test_gateway_provider.py` - провайдер шлюза на `FakeServer(layout=GATEWAY_LAYOUT)`: allowlist всех SSH-команд
  регулярками, флаги `--sysctl` и `bash .../start.sh` в `docker run`, порядок записи (конфиг, скрипт, затем контейнер),
  права 0700/0600, идемпотентность и починка, откат первой установки, чужой контейнер, порт не совпадает с занятым,
  health (нет выходов / kill-switch), клиент из `10.77.0.0/22`, `vpn://`. Текст `start.sh` проверяется по ключевым
  строкам и порядку, плюс `bash -n` (пропускается без bash). Само действие скрипта - только живым прогоном
  (см. `src/infrastructure/vpn/CLAUDE.md`).
- `integration/test_gateway_api.py` - тело POST/DELETE `/protocol`, `Job.params`, воркер (установка, повтор, снятие,
  чужой контейнер, неверный тип в params), клиент на шлюзе, блокировка снятия при живых клиентах (шлюз и обычный
  независимы), sync/health. Фейк с шлюзом: `world.fakes[server.id] = FakeServer(host=..., layout=GATEWAY_LAYOUT)`
  после `world.server(install=False)`. На одном `FakeServer` бывает только одна раскладка «своего» контейнера:
  обычный протокол и шлюз вместе в фейке не моделируются (рядом можно положить лишь чужой контейнер вручную).
- Фейк умеет для шлюза: `docker ps -a --format '{{.Ports}}'` (порты запущенных контейнеров, `container_ports`,
  `host_busy_ports` для процессов вне Docker; `docker run` на занятый порт падает), `--sysctl` (`container_sysctls`),
  команду контейнера (`container_commands`), `start.sh` как действие (`ip_rules`, `firewall`, `start_script_runs`;
  скрипт «выполняется», только если файл равен `render_start_script`, иначе контейнер выходит), `ip rule show`,
  `awg show interfaces` (`exit_interfaces`), `start.sh apply`. Прочее - `AssertionError`.
- Откат первой установки, контейнер без конфига, query `?type=` (согласие/расхождение/мусор), `protocol_type` в аудите
  - в тех же двух файлах (`test_gateway_provider.py`, `test_gateway_api.py`); фейк понимает `test -d` и `rm -f` файлов
  шлюза.
- `services/test_migrations.py` - миграция шлюза (значение enum, ORM, downgrade с данными и каскадом, уникальность).

## Выходы шлюза (этап 24.2)

- `gateway_support.py` - общее: `client_config()` (конфиг внутреннего клиента от настоящего `AmneziaWGProvider` на отдельном
  фейке), `exit_tunnel()`, `apply_exits()`, `add_live_exit()` (файл + интерфейс + свежее рукопожатие на фейковом шлюзе).
- `unit/test_gateway_exits.py` - конфиг выхода строка в строку и по правилам (белый список, `Table`, MTU, хуки, отказ от
  враждебного Endpoint и битого конфига без цитат), слоты и таблицы (нет пересечения с 253-255 и `RU_TABLE`), команды
  провайдера по regexp-списку констант, идемпотентность, syncconf вместо пересоздания, откаты при сбоях up/syncconf,
  `remove_exit`, `exit_status`, health, перезапуск контейнера. `integration/test_gateway_exits_api.py` - API, воркер
  (две фазы, повтор, `ServerBusy`, порядок блокировок шлюз -> выход), снятие (в том числе при недоступном выходе), PATCH,
  GET без секретов, аудит, защиты 409, sync выходов и шлюза, health. Сцена: шлюз `FakeServer(layout=GATEWAY_LAYOUT)` и
  выходы с обычной раскладкой - несколько `FakeServer` одновременно через `world.fakes`.
- Фейк умеет для выходов: `awg-quick up|down` по файлу `exits/gwx<N>.conf` (конфиг разбирается: ключи, `Table`, Endpoint),
  `ip link delete dev`, `awg show gwxN latest-handshakes|transfer`, strip -> syncconf (строго команда
  `exit_sync_command`), `rm -f` файлов выходов; `start.sh` поднимает все `exits/*.conf` при запуске контейнера. Сбои:
  `exit_up_fails`, `exit_down_fails`, `exit_sync_fails` (наборы слотов); `exit_handshakes`, `exit_transfer`,
  `exit_configs` (применённые конфиги). Незнакомая команда - `AssertionError`.
- `services/test_migrations.py` - миграция выходов (таблица, ограничения, каскад/RESTRICT, downgrade, цикл).

## Назначение выходов (этап 24.3)

- `unit/test_gateway_assign.py` - балансировщик (`pick_exit`: веса, `max_clients`, нет исправных, ничья, независимость от порядка),
  `assign.rules` строка в строку и проверка адресов/слотов, разбор `iptables -S GW_ASSIGN`, правило метки в конфиге выхода, текст
  `start.sh` (цепочка, три хука, загрузка карты), `apply_assignments` с откатом, `assignments_in_sync`, карта переживает
  перезапуск. `integration/test_gateway_assignments_api.py` - выдача и отзыв на шлюзе (в том числе без выходов и с
  переиспользованным адресом), перенос при снятии выхода (на втором и на последнем), sync (повторное применение, потерянный
  файл, старый start.sh и туннель, сбой сверки), `GET .../assignments`, аудит без адресов. Миграция - в `services/test_migrations.py`.
- Фейк шлюза (`FakeServer`): `assign_chain` (живая цепочка), `assign_test_fails` / `assign_apply_fails`, `assign_applies`;
  `iptables-restore --test --noflush` + применение разбирают файл строго по формату панели (иное - код 2), `iptables -t mangle -S
  GW_ASSIGN` (нет цепочки при старом start.sh - код 1), `start.sh` создаёт цепочку и грузит `assign.rules`; правило `ip rule` метки
  появляется и исчезает вместе с интерфейсом выхода.

## Россия напрямую (этап 24.4)

- `unit/test_geoip_parser.py` - разбор RIPE (степень двойки и нет, соседние блоки, `reserved` / `available`, IPv6, мусор, пороги
  записей и префиксов, крупнее /8, непубличные), эффективный список, порог 20%, `validate_override`. `unit/test_geoip_download.py` -
  https-only, лимит при чтении и по `Content-Length`, не-200, редирект на http, сетевые сбои без подробностей, настройка URL.
  `unit/test_gateway_ru.py` - `ru.prefixes`, `apply-ru.sh` (текст, bash -n, порядок «новое правило - старое - очистка», сбой
  `ip -batch`, пустая старая таблица), `apply_ru` / `ru_status` на фейке (таблицы 200 <-> 201, откат файла, битая строка в
  обход проверки, перезапуск контейнера), `start.sh`. `integration/test_geoip_api.py` - API, воркер, кандидат и
  подтверждение, исключения (в том числе 422 для чужих / непубличных сетей), рассылка по шлюзам без дублей, `POST .../ru/apply`,
  sync / health / переустановка, правило планировщика, аудит. RBAC - строки в `CRITICAL`, миграция - в `services/test_migrations.py`.
- Корневой `conftest.py`: автоиспользуемая `no_geoip_network` подменяет `geoip_service.download_text` на отказ (установка шлюза без
  списка ставит `GEOIP_UPDATE`, он не должен ходить в сеть); тесты, которым нужен ответ источника, подменяют его сами.
- `tests/geoip_support.py` - синтетические файлы RIPE и CIDR нужного размера (`synthetic_networks`: несмежные /24, не склеиваются).
  RIPE подменяется через `monkeypatch.setattr(geoip_service, "download_text", ...)`: сеть тесты не трогают.
- Фейк шлюза: `ru_tables`, `ru_apply_fails`, `ru_applies`, `default_gateway`; `bash apply-ru.sh` разбирает `ru.prefixes` строго
  (негодная строка - код 1, активная таблица не тронута), перезапуск контейнера сбрасывает таблицы, `start.sh` грузит файл.

## Failover и закрепление (этап 24.5)

- `unit/test_gateway_failover.py` - гистерезис (`next_health`) и `plan_failover`: ничья, веса, `max_clients`, нет исправных,
  подозрительный выход, независимость от порядка, закреплённые уезжают и возвращаются через `return_after` (и не раньше), лимит
  возврата 5 за проход, идемпотентность плана. `integration/test_gateway_failover_api.py` - правило `gateway_watch` +
  воркер на фейковом шлюзе (одна плохая проверка / две / восстановление, закреплённые и нет, нет исправных - одно событие аудита,
  повтор без лишних apply, дедуп задач, ошибки SSH, сервер-выход DOWN, не-ACTIVE выходы), `PATCH .../assignments/{id}` и
  `POST .../check` (404 / 409 / 422; ручная проверка не обходит гистерезис), тотальный отказ и «рестарт шлюза» (счётчики не растут,
  одно событие `gateway_all_exits_down`), пропущенное событие «некуда деть», параллельность и срок правила `gateway_watch`, порядок
  `_ready` -> БД в PATCH, 502 без деталей, аудит, `GET .../exits` без счётчика. «Рукопожатие устарело» -
  `FakeServer.exit_handshakes[slot] = <старое время>`. RBAC - строки в `CRITICAL`, миграция - в `services/test_migrations.py`.

## Правила

- Гонки проверять детерминированно (окно через monkeypatch), а не через `asyncio.gather` по ASGI.
- Админ для интеграции — фикстура `admin` (superadmin), `make_admin`/`login_client` для других ролей.
- Известный плавающий тест: `test_login_limited_by_ip` (окно лимита 60 с, Argon2 под нагрузкой).
