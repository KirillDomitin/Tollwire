# Тесты шлюза: этапы 24.1–24.5 (файлы, что ловят, что умеет фейк шлюза)

Перенесено из `tests/CLAUDE.md` без изменений текста; там остались окружение, правила и основа фейка SSH.

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
