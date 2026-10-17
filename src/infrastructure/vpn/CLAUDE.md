# infrastructure/vpn — VPN-провайдер (AmneziaWG)

Бизнес-логика знает только интерфейс `VPNProvider` (`provider.py`). Всё про Docker, пути, ключи и формат
конфигов — здесь. Модули сюда не импортируются; фабрика провайдера по `ProtocolType` — `provider_class()` в
`modules/servers/service.py`.

| Файл | Что внутри |
|---|---|
| `provider.py` | `VPNProvider` (ABC) и структуры: `ProviderState`, `ProviderStatus`, `VPNClientInfo`, `VPNClientConfig` (config с `repr=False`), `ProviderHealth`, `SyncResult`. Общая логика: `client_counts(now)` (всего / онлайн: рукопожатие за `ACTIVE_HANDSHAKE_WINDOW_SECONDS` = 180 с), `share_link`/`share_qr_codes` (по умолчанию None), `discover()` (по умолчанию None: тип не участвует в поиске), структуры `Discovery`/`DiscoveredClient`. |
| `amnezia.py` | `AmneziaInstaller` (готовность сервера: root + Linux + Docker, `ensure_ready`, версия Docker, задержка до интернета) и `AmneziaWGProvider` (протокол). |
| `awg_config.py` | Чистые функции без SSH: ключи (X25519 + PSK, генерируются **локально**, не на сервере), порт, случайная обфускация, рендер `awg0.conf`, разбор секций, `[Peer]`-блоки, аллокатор адресов, NAT-хуки, конфиг клиента, разбор рукопожатий, разбор `clientsTable` и `[Peer]` для поиска (`discover_peers`). |
| `amnezia_app.py` | `AmneziaAppAWGProvider` и `AmneziaAppAWG2Provider` — установка приложения AmneziaVPN: чтение, отзыв клиента и возврат peer'а (см. раздел ниже). |
| `amnezia_link.py` | Ссылка `vpn://` и серия QR в формате приложения AmneziaVPN. |
| `config.py` | Константы: имя контейнера, образ по дайджесту, пути, подсеть, NAT, DNS клиента, шаблон конфига. |

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

## Чужая установка приложения AmneziaVPN (`amnezia_app.py`)

Приложение AmneziaVPN ставит контейнеры с теми же именами, что и панель. Классы приложения читают контейнер и меняют
только peer'ы клиентов (`revoke_client`, `restore_peer`). Их выбирает `provider_class()` для `AMNEZIA_APP_AWG` и
`AMNEZIA_APP_AWG2`.

| Класс | Контейнер | Конфиг в контейнере | Интерфейс | Утилита |
|---|---|---|---|---|
| `AmneziaAppAWGProvider` | `amnezia-awg` | `/opt/amnezia/awg/wg0.conf` | `wg0` | `wg` |
| `AmneziaAppAWG2Provider` | `amnezia-awg2` | `/opt/amnezia/awg/awg0.conf` | `awg0` | `awg` |

- Контейнер ищется по точному имени (`docker ps -a --filter 'name=^/?…$'`). Контейнер, смонтировавший
  `/opt/vpn-admin/awg` (`HOST_CONFIG_DIR`), — панельный, не приложения: для этих классов он «не установлен».
- Чтение — `docker exec <контейнер> cat <путь>` (конфиг и `/opt/amnezia/awg/clientsTable`) и
  `docker exec <контейнер> <wg|awg> show <интерфейс> latest-handshakes`. Изменения — только протоколом записи ниже.
- `install`, `uninstall`, `create_client`, `restore_client` → `UnsupportedOperationError` (`core/ssh_exception.py`).
  `restore_client` (клиент с приватным ключом из конфига) здесь не работает: у панели нет приватного ключа клиента,
  возврат — `restore_peer`. До этих операций API не доходит: эндпоинты протокола работают только с `AMNEZIA_WG`.
- `list_clients()`, `sync()` и `discover()` читают через общий `_read()`. `discover()` возвращает `Discovery`
  (контейнер, состояние, образ, порт, клиенты: ключ, адрес, имя, последнее рукопожатие, `supported`).
- `clientsTable` — JSON в двух формах: список `{clientId, clientName}` или объект `{clientId: {clientName}}`; имя
  может лежать в `userData`. `parse_clients_table`: длиннее 1 МиБ (в символах) или не JSON → `{}`; из имени удаляются управляющие,
  невидимые и разделители строк (Unicode-категории Cc, Cs, Cf, Zl, Zp), длина ≤ 100.
- `discover_peers` (`awg_config.py`) берёт из серверного конфига `[Peer]`-ы: `PublicKey` (невалидный — пропуск) и
  `AllowedIPs`. Один IPv4 `/32` — адрес; иначе адреса нет и `supported=False` (несколько адресов, IPv6, не `/32`).
- Из серверного конфига берутся `ListenPort` и поля `[Peer]` выше. `PrivateKey` и `PresharedKey` в структуры,
  `Discovery` и отчёт не попадают.

### Запись: `revoke_client`, `restore_peer`

- `revoke_client(public_key)`: невалидный ключ — `ValueError`. Peer'а с этим ключом нет — файл не пишется, интерфейс
  выравнивается по файлу (`_apply`). Затем запись клиента в `clientsTable` удаляется (если есть).
- `restore_peer(name, public_key, psk, address)`: невалидные ключи — `ValueError`. Peer с этим ключом уже есть — `False`,
  файл не пишется (интерфейс выравнивается, как выше). Иначе `[Peer]` в формате приложения, без комментария `# client:`
  (`render_app_peer_block`): `PublicKey`, `PresharedKey`, `AllowedIPs = <адрес>/32`. Адрес — IPv4 внутри подсети
  (первый IPv4 из `Address` серверного `[Interface]`, не константа); сетевой адрес и адрес интерфейса — `ValueError`,
  broadcast — `ValueError`, занят другим peer'ом — `CommandExecutionError`. Имя — `validate_name` (`[a-zA-Z0-9_-]`,
  1–32 символа). В `clientsTable` добавляется `{clientId, userData: {clientName}}`, только если записи о ключе там нет:
  настоящее имя приложения не перетирается.

### Протокол записи (`_edit_config`)

Вызывающий держит ServerLock. Шаги:

1. `_require_running`: контейнер найден, он не панельный и запущен; иначе `CommandExecutionError`.
2. `docker exec <к> cat <конфиг>` → `original`. Не читается → `CommandExecutionError`.
3. `change(original)` в памяти. `None` — менять нечего: файл не пишется, но шаг 6 выполняется (возврат `False`).
4. Перечитывание: конфиг уже не равен `original` (его поменяли между чтениями, например приложение AmneziaVPN) → `ConfigChangedError`, ничего не записано.
5. Запись: `mkdir -p /opt/vpn-admin`; `ensure_private_dir` для `/opt/vpn-admin/stage` (0700); `rm -f /opt/vpn-admin/stage/<контейнер>.*`
   (остатки прерванной операции, в том числе временные файлы SFTP); SFTP-файл `/opt/vpn-admin/stage/<контейнер>.conf`
   (0600); `docker cp` в контейнер; `rm -f` файла на хосте — в `finally`, при любом исходе.
6. Применение (`_apply`): `docker exec <к> bash -c 'c=$(<утилита>-quick strip <конфиг>) && [ -n "$c" ] && <утилита> syncconf <интерфейс> <(printf "%s\n" "$c")'`.
7. Сбой применения (любое исключение, в том числе отмена) → `_rollback`: если файл в контейнере всё ещё равен новому, прежний
   конфиг пишется тем же путём и применяется снова; если файл изменило приложение, откат пропускается (`error` в логе).
   Ошибка отката логируется, исходное исключение идёт наверх.

Окно между перечитыванием (шаг 4) и записью неустранимо: проверка сужает гонку с приложением, но не закрывает её.

`clientsTable` (`_edit_clients_table`): только после успешного применения и только если файл — JSON-массив (объект
старого формата не переписываем; больше 1 МиБ — не трогаем). Сбой чтения, разбора или записи — `warning` в логе, не
ошибка: доступ важнее таблицы.

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
