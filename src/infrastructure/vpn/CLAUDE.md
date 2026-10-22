# infrastructure/vpn — VPN-провайдер (AmneziaWG)

Бизнес-логика знает только интерфейс `VPNProvider` (`provider.py`). Всё про Docker, пути, ключи и формат
конфигов — здесь. Модули сюда не импортируются; фабрика провайдера по `ProtocolType` — `provider_class()` в
`modules/servers/service.py`.

**Подробности — читать перед работой над соответствующей частью:**
- шлюз, выходы, назначение выходов, «Россия напрямую», failover (этапы 24.1–24.5, живые проверки) —
  `docs/vpn/gateway.md`;
- чужая установка приложения AmneziaVPN, отзыв/возврат peer'а, протокол записи — `docs/vpn/amnezia-app.md`;
- полная карта файлов (поля структур, `ContainerLayout`) — `docs/vpn/files.md`.

| Файл | Что внутри |
|---|---|
| `provider.py` | `VPNProvider` (ABC) и структуры (`ProviderStatus`, `VPNClientInfo`, `VPNClientConfig` с `repr=False`, `ProviderHealth`, `SyncResult`, `Discovery`, `ImportablePeer` — PSK не в repr); `client_counts(now)` — онлайн = рукопожатие за 180 с. |
| `amnezia.py` | `AmneziaInstaller` (root + Linux + Docker, `ensure_ready`) и `AmneziaWGProvider` (протокол). |
| `awg_config.py` | Чистые функции без SSH: ключи (генерируются **локально**), порт, обфускация, рендер и разбор `awg0.conf`, аллокатор адресов, NAT-хуки, конфиг клиента, рукопожатия, `clientsTable`, `discover_peers` / `importable_peers`. |
| `amnezia_app.py` | `AmneziaAppAWGProvider`, `AmneziaAppAWG2Provider` — установка приложения AmneziaVPN (`docs/vpn/amnezia-app.md`). |
| `amnezia_link.py` | Ссылка `vpn://` и серия QR в формате приложения. |
| `config.py` | Константы (контейнер, образ по дайджесту, пути, подсеть, NAT, DNS) и `ContainerLayout`; `AmneziaWGProvider.layout` — атрибут класса, подкласс (шлюз) переопределяет его. Команды раскладки по умолчанию закреплены golden-файлом `tests/unit/golden/provider_default_commands.json`. |
| `gateway.py` | `AmneziaGatewayProvider(AmneziaWGProvider)` — шлюз и его выходы. |
| `gateway_config.py` | `GATEWAY_LAYOUT`, sysctl, `start.sh`, `apply-ru.sh`, `assign.rules`, конфиги выходов, разборщики вывода. Чистые функции без SSH. |
| `split_routes.py` | Какие сети можно класть в таблицу «Россия напрямую» — единый источник для `modules/geoip` и шлюза. |

## Как устроено на сервере

- Один контейнер на протокол: `amnezia-awg` (фильтр `name=^/?amnezia-awg$` — точное имя, иначе зацепит чужой
  `amnezia-awg2`), образ `amneziavpn/amneziawg-go@sha256:…`, `--cap-add=NET_ADMIN --device=/dev/net/tun`,
  `--restart unless-stopped`, UDP-порт наружу, папка `/opt/vpn-admin/awg` смонтирована в `/etc/amnezia/awg`.
- Внутри: `awg-quick up awg0.conf && tail -f /dev/null` — упал awg-quick → контейнер сразу выходит.
- Подсеть клиентов `10.8.1.0/24` (сервер .1), NAT — `PostUp/PostDown` с MASQUERADE (PostUp идемпотентен `-C || -A`).
- Чужие контейнеры на сервере не трогаем: всё своё — по своим именам и папкам.
- **Имя `amnezia-awg` совпадает с контейнером приложения AmneziaVPN (v1).** Контейнер «наш», только если он есть
  и среди его монтирований (`docker inspect --type container -f '{{range .Mounts}}{{.Source}} {{end}}'`) точно
  `/opt/vpn-admin/awg` (`_is_foreign`, сравнение по токенам, не по префиксу). Для чужого: `install`,
  `create_client`, `revoke_client`, `restore_client` → `ForeignContainerError` (подкласс `DockerError`, 
  `core/ssh_exception.py`), `uninstall` ничего не меняет и возвращает «не установлен», `get_status`/`list_clients`/
  `health_check`/`sync` — «не установлен» без `docker exec` и чтения конфига. Сбой `docker ps`/`inspect` — ошибка
  (`CommandExecutionError`), а не «контейнера нет». `_run_container`/`_rm_container` перепроверяют владельца сами.
  Задача отзыва доступа на сервере, где контейнер стал чужим, падает с этой ошибкой (понятной) и повторяется.

## Инварианты шлюза (подробности и причины — `docs/vpn/gateway.md`)

- Контейнер `vpn-admin-gw`, папка `/opt/vpn-admin/gw` (0700, файлы 0600), подсеть клиентов `10.77.0.0/22`.
  Отдельный от обычного протокола; подкласс меняет только `layout`, `_docker_run_command`, `install`, `health_check`.
- В образе только iproute2, iptables (nft), bash, awg, awg-quick, busybox `flock`. Нет nft, ipset, conntrack-утилиты,
  sysctl, curl, `/etc/iproute2` — таблицы только числами.
- Приоритеты `ip rule`: **100** «Россия» (таблицы 200/201) < **1000** метки выходов < **9000** `prohibit` (kill-switch).
  Правила ставятся **до** подъёма `gw0`; только «проверить → add», никогда `del` + `add` (окно без kill-switch).
- Таблица и метка выхода — **1000 + слот** (слот 1..200), не 100 + слот: иначе 253–255 и 200 заняты.
  Из слота строятся только имя `gwx<N>`, файл и таблица; все команды константные.
- Конфиг выхода собирается по белому списку ключей; Endpoint проверяется; DNS/PostUp/Table из чужого конфига отбрасываются.
- `start.sh`, `apply-ru.sh` — константы панели, перезаписываются при расхождении. Всё в них идемпотентно.
- Назначение клиентов: `iptables -t mangle` цепочка `GW_ASSIGN` + CONNMARK (старые соединения остаются на прежнем
  выходе); применение — `iptables-restore -w --test`, затем без `--test`; сбой — возврат прежнего файла.
- «Россия напрямую»: две таблицы 200/201, наполнение запасной одним `ip -batch`, затем add нового правила 100,
  del старого — ни мгновения без правила. Список сетей — только через `split_routes.py`.
- `rp_filter=0` (не 2): иначе ответы выходов режутся проверкой обратного пути. Контейнер со старыми sysctl
  `install()` пересоздаёт (аудит `gateway_container_recreated`).
- Первая установка при сбое откатывает только созданное этим вызовом. Конфига нет, а контейнер есть — ошибка, ничего
  не пишется.
- Вызывающий держит `ServerLock` шлюза. stderr команд с конфигами в исключения и логи не попадает.

## Инварианты приложения AmneziaVPN (подробности — `docs/vpn/amnezia-app.md`)

- Контейнеры `amnezia-awg` (`wg`/`wg0`) и `amnezia-awg2` (`awg`/`awg0`), поиск по точному имени; контейнер с
  монтированием `/opt/vpn-admin/awg` — панельный, для этих классов «не установлен».
- Меняются только peer'ы (`revoke_client`, `restore_peer`); `install`/`uninstall`/`create_client`/`restore_client` —
  `UnsupportedOperationError`.
- Запись только через `_edit_config`: чтение → изменение в памяти → перечитывание (изменилось — `ConfigChangedError`)
  → stage-файл 0600 + `docker cp` → `_apply` → откат при сбое. `clientsTable` — после успеха, ошибки в ней не роняют операцию.
- `PrivateKey` не попадает никуда; `PresharedKey` — только в `ImportablePeer` (импорт), не в `Discovery` и отчёты.

## Операции провайдера

- `install()` — идемпотентен: если `awg0.conf` уже есть, ключи и порт **не** перегенерируются; старые установки
  дочиниваются: NAT-хуки и политика перезапуска (`docker update --restart unless-stopped`, без перезапуска
  контейнера; `always` и `unless-stopped`, выбранные вручную, не трогаем). После `docker run` — пауза перед
  проверкой «running» (контейнер может упасть позже).
- `create_client(name)` — SFTP-чтение `awg0.conf` → свободный адрес → ключи → `[Peer]` с комментарием
  `# client:<name>` → запись → `awg-quick strip` + `awg syncconf` (без перезапуска). Конфиг клиента: обфускация
  копируется из серверного `[Interface]` по allowlist (`_CLIENT_INTERFACE_KEYS`), PostUp/PostDown не копируются.
- `revoke_client(public_key)`, `restore_client(name, config)` — идемпотентны.
- `list_clients()`, `sync()` (снимок без изменений), `health_check()` (в том числе `restart_policy`: без неё VPN не поднимется после перезагрузки сервера), `get_status()`, `uninstall()`.
- Имя клиента — белый список `[a-zA-Z0-9_-]{1,32}` (`validate_name`, `re.fullmatch` — `$` пропускает `\n`).

## Формат vpn:// и QR

- `vpn://` + base64url без `=` от (4 байта длины big-endian + zlib(JSON)) — как `qCompress` в AmneziaVPN.
  JSON: `containers[0]` = `amnezia-awg2` с параметрами обфускации и `last_config`; `description` = имя сервера в
  панели (его показывает приложение).
- QR — серия кусков по 850 байт: заголовок `>hBBI` (magic 1984, число кусков, номер, длина) + байты, base64url,
  коррекция ошибок LOW. Один плотный QR камера не читала — только серия.

## Грабли (все пойманы живыми прогонами)

- Проверять stdout, а не exit-код (`docker ps`, пайпы); `strip()` вывода.
- Путь-директория и путь-файл — разные константы (bind-mount файла вместо папки ломал awg-quick).
- Генерация ключей/порта — только внутри `install()`, не в `__init__`.
- Долгим командам — свой таймаут, а не общий `SSH_COMMAND_TIMEOUT` (30 с): `apt-get install docker.io` — 600 с
  (`DOCKER_INSTALL_TIMEOUT`), `docker run` — 300 с. Таймаут обрывает ожидание, а не команду: apt на сервере
  продолжает работу. Сумма должна оставаться меньше `STALE_JOB_TIMEOUT_SECONDS` (1800 с).
- Логика AmneziaWG проверяется только на реальном сервере — фейк в тестах отвечает лишь на известные команды.
- Вывод `cat` конфига приложения содержит `PrivateKey` и `PresharedKey` — не логировать, не класть в исключения и
  отчёты. Вывод `wg show` и `clientsTable` — недоверенный ввод с сервера: лимиты и пропуск кривых строк вместо
  исключения (`parse_latest_handshakes`, `parse_clients_table`).
- `syncconf <(strip)` самого приложения при упавшем `strip` получает ПУСТОЙ конфиг, возвращает код 0 и СНОСИТ ВСЕХ
  peer'ов с живого интерфейса (воспроизведено на контейнере). Поэтому `_apply` сначала проверяет `strip`
  (`&& [ -n "$c" ]`); штатный вызов `syncconf <(strip)` в панели не использовать.
- stderr `docker cp` и `docker exec` в исключения не попадает: `wg` цитирует строку конфига с ошибкой, а в конфиге
  `PrivateKey`.
- Промежуточные файлы `/opt/vpn-admin/stage` — приватный ключ сервера: каталог 0700, файл 0600, удаляются сразу.
  Остатки прерванной операции `rm -f` чистит перед записью, а не полагается на прошлый `finally`.
