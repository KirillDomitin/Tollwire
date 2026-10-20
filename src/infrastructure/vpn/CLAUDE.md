# infrastructure/vpn — VPN-провайдер (AmneziaWG)

Бизнес-логика знает только интерфейс `VPNProvider` (`provider.py`). Всё про Docker, пути, ключи и формат
конфигов — здесь. Модули сюда не импортируются; фабрика провайдера по `ProtocolType` — `provider_class()` в
`modules/servers/service.py`.

| Файл | Что внутри |
|---|---|
| `provider.py` | `VPNProvider` (ABC) и структуры: `ProviderState`, `ProviderStatus`, `VPNClientInfo`, `VPNClientConfig` (config с `repr=False`), `ProviderHealth`, `SyncResult`. Общая логика: `client_counts(now)` (всего / онлайн: рукопожатие за `ACTIVE_HANDSHAKE_WINDOW_SECONDS` = 180 с), `share_link`/`share_qr_codes` (по умолчанию None), `discover()` (по умолчанию None: тип не участвует в поиске), структуры `Discovery`/`DiscoveredClient`, `ImportablePeer` (клиент для импорта; PSK не в repr), `read_importable()` (по умолчанию `UnsupportedOperationError`). |
| `amnezia.py` | `AmneziaInstaller` (готовность сервера: root + Linux + Docker, `ensure_ready`, версия Docker, задержка до интернета) и `AmneziaWGProvider` (протокол). |
| `awg_config.py` | Чистые функции без SSH: ключи (X25519 + PSK, генерируются **локально**, не на сервере), порт, случайная обфускация, рендер `awg0.conf`, разбор секций, `[Peer]`-блоки, аллокатор адресов, NAT-хуки, конфиг клиента, разбор рукопожатий, разбор `clientsTable`, `[Peer]` для поиска (`discover_peers`) и для импорта с PSK (`importable_peers`, `ImportedPeer`). |
| `amnezia_app.py` | `AmneziaAppAWGProvider` и `AmneziaAppAWG2Provider` — установка приложения AmneziaVPN: чтение, отзыв клиента и возврат peer'а (см. раздел ниже). |
| `amnezia_link.py` | Ссылка `vpn://` и серия QR в формате приложения AmneziaVPN. |
| `config.py` (раскладка) | `ContainerLayout` — неизменяемая «раскладка» контейнера: имя, папка конфига на хосте и в контейнере, файл, интерфейс, подсеть клиентов, DNS клиентов, необязательный `server_address` (адрес интерфейса, если он не равен подсети; у шлюза 10.77.0.1/22). `DEFAULT_LAYOUT` = прежние значения. `AmneziaWGProvider.layout` — атрибут класса: подкласс (шлюз) переопределяет его, и все команды, проверка «наш ли контейнер» (23.1), NAT, health и ссылка `vpn://` берутся из него. Для раскладки по умолчанию команды те же байт в байт (`tests/unit/golden/provider_default_commands.json`). `share_link`/`share_qr_codes` — classmethod. |
| `config.py` | Константы: имя контейнера, образ по дайджесту, пути, подсеть, NAT, DNS клиента, шаблон конфига. |
| `gateway.py` | `AmneziaGatewayProvider(AmneziaWGProvider)` - шлюз и его выходы (разделы ниже: «Шлюз», «Выходы шлюза»). |
| `gateway_config.py` | Раскладка шлюза `GATEWAY_LAYOUT`, флаги `sysctl`, текст `start.sh` (`render_start_script`), разбор `ip rule show` / `awg show interfaces` / `docker ps {{.Ports}}`. Чистые функции без SSH. Выходы (24.2): `ExitTunnel`, `ExitTunnelStatus`, `render_exit_config`, имена/таблицы по слоту, константные команды, `exit_needs_restart`, `exit_health_label`. |

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

## Шлюз (`gateway.py`, `gateway_config.py`, этап 24.1)

Шлюз - российский сервер, к которому клиенты подключаются одним конфигом; он завершает клиентский AWG и пересылает
трафик на зарубежные серверы панели («выходы», 24.2). Это второй, независимый контейнер рядом с обычным протоколом
(`ProtocolType.AMNEZIA_GATEWAY`, `provider_class`). Подкласс меняет только `layout` и три места базового класса:
`_docker_run_command`, `install`, `health_check`; всё остальное (клиенты, sync, NAT-хуки, защита 23.1, ссылка `vpn://`)
берётся из раскладки.

| Поле раскладки | Значение |
|---|---|
| контейнер | `vpn-admin-gw` (образ тот же закреплённый `AMNEZIAWG_IMAGE`) |
| папка на хосте / в контейнере | `/opt/vpn-admin/gw` / `/etc/vpn-admin/gw` (папка `0700`, файлы `0600`) |
| конфиг / интерфейс | `gw0.conf` / `gw0` |
| подсеть клиентов | `10.77.0.0/22`, сервер `10.77.0.1/22`, клиенты с `10.77.0.2` |
| DNS клиентов | `1.1.1.1, 1.0.0.1` (идёт через туннель и выход) |

- `docker run`: как у обычного плюс `--sysctl net.ipv4.ip_forward=1`, `net.ipv4.conf.{all,default}.rp_filter=2`,
  `net.ipv6.conf.all.disable_ipv6=1`; команда контейнера `bash /etc/vpn-admin/gw/start.sh`.
- В образе есть только iproute2, iptables (nft-бэкенд), bash, awg, awg-quick. nftables, ipset, dnsmasq, conntrack,
  sysctl, curl там нет - ими пользоваться нельзя. Таблица маршрутов `200` (будущая «ru») задана числом: `/etc/iproute2`
  в образе нет, имя `ru` не разрешится.
- `start.sh` - константа панели (`render_start_script`), пишется по SFTP в `/opt/vpn-admin/gw/start.sh` (0600, запуск
  через `bash`, chmod не нужен) и перезаписывается при каждой установке, если отличается. Порядок внутри: правила
  `ip rule` и изоляция клиентов **до** подъёма `gw0` (kill-switch действует с первой секунды), затем `awg-quick up gw0`
  (упал - код 1, restart policy перезапускает), затем туннели `exits/*.conf` (упавший выход контейнер не роняет,
  уже поднятые пропускаются), затем MSS (недоступен на хосте - предупреждение, не отказ), затем `tail -f /dev/null`
  (с `trap`: `docker stop` занимает меньше секунды). Режим `start.sh apply` - то же без `gw0` и без ожидания, для
  работающего контейнера.
- Правила: `ip rule 100 from 10.77.0.0/22 lookup 200` (таблица пуста до 24.4 - RPDB идёт дальше),
  `ip rule 9000 from 10.77.0.0/22 prohibit` (kill-switch: трафик клиентов не попадает в main - нет утечки зарубежного
  с российского IP, нет доступа к хосту и docker-сети), `iptables FORWARD -i gw0 -o gw0 -j DROP` (клиенты изолированы),
  `mangle FORWARD ... TCPMSS --clamp-mss-to-pmtu`. NAT (`MASQUERADE` для российского напрямую) даёт PostUp в
  `gw0.conf` - скрипт его не повторяет. Все правила идемпотентны: перед `add` - проверка (`ip rule show | grep`,
  `iptables -C`); `ip rule del` + `add` не используем - между ними kill-switch на миг пропал бы.
- `install()`: чужой контейнер -> `ForeignContainerError`; `mkdir -p /opt/vpn-admin`, `ensure_private_dir` папки шлюза;
  конфиг есть - порт читается из него, ключи не трогаются, `start.sh` дописывается при отличии, контейнер создаётся
  (нет) / запускается (остановлен) / получает NAT-хук, restart policy и `start.sh apply` (работает). Конфига нет, а
  контейнер есть (любое состояние) - `CommandExecutionError` "container exists but ... is missing", ничего не
  пишется и не откатывается: новый конфиг получил бы другой порт, чем у работающего контейнера. Нет ни того, ни другого
  - первая установка: порт, ключи, запись `gw0.conf`, запись `start.sh`, `docker run` (порядок важен: без скрипта
  контейнер сразу выходит). Сбой первой установки откатывает только созданное этим вызовом (флаги `created.dir /
  container / files`): контейнер (`docker stop && docker rm`), папку целиком (`rm -rf`, если её создали мы) или только
  свои `gw0.conf` и `start.sh` (`rm -f`, если папка уже была). Иначе повторная установка нашла бы готовый конфиг с
  неудачным портом. У обычного протокола отката нет.
- Порт: UDP, случайный, и не опубликованный ни одним контейнером сервера. Занятые порты читает константная команда
  `docker ps -a --format '{{.Ports}}'` (а не БД: так видны и контейнеры, о которых панель не знает); до 100 попыток.
  Ограничение: остановленный контейнер порт не показывает, процесс вне Docker - тоже (тогда `docker run` упадёт и
  установка откатится). Обычный протокол порт не сверяет.
- Известные ограничения шлюза 24.1: `exits` не проходит, пока нет выходов (сервер DEGRADED; с 24.2 выходы подключаются через API); `disable_ipv6`
  sysctl на хосте без IPv6 может не примениться (тогда `docker run` откатится - проверять на таком хосте); клиент не
  пингует `10.77.0.1`; порт остановленного контейнера и процесса вне Docker при выборе порта не виден; в одном
  `FakeServer` одна раскладка (шлюз и обычный протокол вместе в фейке не моделируются); тип протокола во
  фронтенде (`schema.d.ts`, ServerPage) обновится в 24.6.
- `health_check()`: проверки базового плюс (только если контейнер запущен) `kill_switch` (`docker exec ... ip rule show`,
  есть `9000: from <подсеть> prohibit`) и `exits` (с 24.2: хотя бы один туннель `gwxN` с интерфейсом и свежим рукопожатием, см. «Выходы шлюза»).
  Пока выходов нет, `exits` не пройдена ("no exit tunnels") - сервер DEGRADED.
- Живая проверка 24.1 (локальный Docker, настоящий провайдер, образ из `config.py`): контейнер `running`, sysctl применены
  (`/proc/sys`), `ip rule` с приоритетами 100 и 9000, FORWARD DROP, MSS и MASQUERADE на месте, повторная установка и
  `start.sh apply` ничего не плодят, после `docker restart` правила и peer'ы возвращаются, права 0700/0600. Клиентский
  контейнер с конфигом из `create_client` дал рукопожатие с `gw0`; через туннель `ping 1.1.1.1` и `ping` хоста docker-сети
  не проходят, а без правила 9000 (контроль) проходят - kill-switch именно он. Битый `gw0.conf` - контейнер выходит
  и перезапускается; битый `exits/bad.conf` контейнер не роняет. Чего нет: реальных выходов (24.2) и маршрутов «РФ
  напрямую» (24.4); на Docker Desktop ядро без модуля AmneziaWG - работает userspace `amneziawg-go`, на сервере с
  модулем поведение то же.
- Расхождения с допущениями дизайна: правила `ip rule` ставятся до `gw0`, а не после выходов (иначе окно утечки при
  старте); сами клиенты не могут пинговать `10.77.0.1`: ответ с адреса из подсети клиентов тоже попадает под prohibit
  (DNS идёт на 1.1.1.1 через выход, службы на шлюзе не нужны); правило FORWARD DROP в 24.1 не срабатывает (клиент -
  клиент запрещён раньше, на prohibit), оставлено как второй рубеж для 24.3+.

## Выходы шлюза (`gateway.py`, `gateway_config.py`, этап 24.2)

Выход - зарубежный сервер панели с протоколом `AMNEZIA_WG`. Шлюз - клиент выхода: на выходе панель создаёт
«внутреннего клиента» обычным `create_client`, а на шлюзе поднимает AWG-интерфейс `gwx<слот>` с его конфигом
(логика, БД и API - `modules/gateway/CLAUDE.md`). Здесь только провайдер и чистые генераторы (без SSH).

| Что | Где |
|---|---|
| `ExitTunnel(slot, client_config [repr=False], endpoint)` | вход `apply_exits`; `endpoint` - `host:port` выхода (`format_endpoint`) |
| `ExitTunnelStatus(slot, interface_up, latest_handshake, rx_bytes, tx_bytes)` | результат `exit_status()` - ключей нет |
| `render_exit_config(tunnel)` | конфиг для `awg-quick` из конфига внутреннего клиента |
| `apply_exits(exits)`, `remove_exit(slot)`, `exit_status(expected=())` | методы `AmneziaGatewayProvider`; вызывающий держит `ServerLock` шлюза |

- **Слот** - целое 1..200 (`_slot`: bool, строки, вне диапазона - `ValueError`). Из него строятся ТОЛЬКО интерфейс
  `gwx<слот>`, файл `/opt/vpn-admin/gw/exits/gwx<слот>.conf` (в контейнере `/etc/vpn-admin/gw/exits/…`) и таблица
  `1000 + слот`. Все команды константные (`exit_up_command`, `exit_down_command`, `exit_sync_command`, `exit_show_command`,
  `exit_delete_link_command`), пользовательского ввода в них нет.
- **Таблица маршрутов `1000 + слот`, а не `100 + слот`** (расхождение с дизайном): `100 + слот` при слотах 153-155 даёт
  таблицы 253/254/255 (default/main/local - `ip route add … table 254` изменил бы main хоста), а слот 100 - таблицу 200
  (`RU_TABLE`, российское напрямую). База `EXIT_TABLE_BASE = 1000` обходит все зарезервированные номера (проверяется
  тестом по всем 200 слотам). Выбор выходов клиентами (24.3) строится на тех же функциях.
- **Конфиг выхода**: из `[Interface]` берутся `PrivateKey`, `Address` (`/32` внутреннего клиента) и обфускация по тому же
  белому списку `_CLIENT_INTERFACE_KEYS` (`obfuscation_params`; значение - одна строка `[\w<>=,:+/ .-]`); DNS, `PostUp`,
  `Table`, `SaveConfig` и всё остальное из чужого конфига отбрасывается. Добавляются `MTU = 1280`, `Table = 1000+слот`,
  `PostUp` `iptables -t nat -C … || iptables -t nat -A POSTROUTING -o gwxN -j MASQUERADE` (идемпотентно) и `PostDown`
  `-D … 2>/dev/null || true` (уже снятое правило не срывает `awg-quick down`, в нём `set -e`). `[Peer]`: `PublicKey`,
  `PresharedKey`, `Endpoint`, `AllowedIPs = 0.0.0.0/0`, `PersistentKeepalive = 25`. `Endpoint` строится из `host` сервера,
  который вводит админ, и проверяется (имя хоста / IPv4 / IPv6 в скобках + порт 1..65535; пробел, перевод строки, `#` -
  `ValueError`): иначе в файл попала бы лишняя строка.
- **`apply_exits`**: нужен работающий свой контейнер (иначе `CommandExecutionError`, чужой - `ForeignContainerError`).
  Файл пишется SFTP прямо в `exits/` (атомарно, 0600; каталог 0700 через `ensure_private_dir`; `/opt/vpn-admin/gw` уже
  смонтирован в контейнер, `docker cp` не нужен). Для каждого туннеля: интерфейса нет -> запись файла и
  `awg-quick up`; конфиг тот же и интерфейс поднят -> ничего; поменялись ключи/обфускация/Endpoint -> запись и
  `syncconf` (безопасный вариант 23.3: `awg-quick strip файл > /tmp/gwxN.stripped && awg syncconf gwxN …; rm -f`);
  поменялись `Address`/`Table`/`MTU`/хуки (`exit_needs_restart`) -> `awg-quick down` по СТАРОМУ файлу, затем запись и
  `up`. Сбой откатывает: у нового выхода файл удаляется, у существующего восстанавливается старый конфиг (и при
  пересоздании поднимается старый туннель), так что файл не остаётся без интерфейса и наоборот. stderr команд в
  исключение не попадает (там цитаты конфига с ключом).
- **Временный `/tmp/gwxN.stripped`** (в нём приватный ключ) создаётся под `umask 077` (в начале скрипта `sh -c`).
- **Удаление шлюза** (`uninstall`, базовый `_rm_container`) делает `rm -rf` всей папки `/opt/vpn-admin/gw` вместе с `exits/`, а
  интерфейсы уходят с контейнером: отдельной чистки `exits/gwx*.conf` не нужно (тест `test_uninstalling_the_gateway_removes_the_exit_files…`).
- **`remove_exit(slot)`**: `awg-quick down` по файлу (нет файла - сначала `iptables -t nat -D POSTROUTING -o gwxN -j MASQUERADE || true`, затем `ip link delete dev gwxN`: PostDown без файла не выполнится), затем `rm -f`; упавший `down`
  оставляет пару (файл, интерфейс) целой; повтор - не ошибка.
- **`exit_status(expected)`**: `awg show interfaces` -> для `gwx<N>` `awg show gwxN latest-handshakes` и `transfer`;
  ожидаемые слоты без интерфейса - `interface_up=False`; чужие имена интерфейсов игнорируются.
- **`health_check`**: `exits` пройдена, если есть хотя бы один туннель с интерфейсом и рукопожатием не старше
  `ACTIVE_HANDSHAKE_WINDOW_SECONDS` (180 с); в деталях - состояние каждого: `2 exit tunnel(s), 1 healthy: gwx1 ok, gwx2 down`
  (метки `exit_health_label`: ok / stale / no_handshake / down).

### Живая проверка 24.2 (локальный Docker, настоящие `AmneziaGatewayProvider` и два `AmneziaWGProvider` с раскладками-подклассами)

- `awg-quick` образа поддерживает `Table = N` (`add_route` кладёт `default dev gwxN` в таблицу N и не создаёт ни `fwmark`-правил, ни
  правил `suppress_prefixlength`): `ip route show table 1001` -> `default dev gwx1 scope link`, `main` и `ip rule` до и
  после не изменились. Имя интерфейса `gwx<N>` проходит проверку awg-quick (до 15 символов).
- Рукопожатия с обоими выходами есть сразу после `apply_exits` (keepalive 25 с), MTU 1280, MASQUERADE на `gwx1`/`gwx2` по
  одному правилу, `rp_filter` у интерфейсов 2.
- **Одинаковый адрес внутренних клиентов** (оба выхода выдали `10.8.1.1`, у них одна подсеть `10.8.1.0/24`): интерфейсы
  `gwx1` и `gwx2` получают один и тот же локальный адрес без ошибок, рукопожатия и маршруты в таблицах 1001/1002 работают.
  Сквозная проверка временным правилом `ip rule from 10.77.0.0/22 lookup 1001|1002` и клиентским контейнером: ping
  `1.1.1.1` проходит через каждый выход по очереди, счётчики `rx/tx` обоих туннелей растут, без правила - потеря 100%
  (kill-switch). Пользователь выбора выхода (24.3) обязан учитывать, что адреса на разных `gwxN` совпадают: выбор идёт по
  таблице/метке, а не по адресу.
- Повторный `apply_exits` с тем же конфигом не выполняет ни одной изменяющей команды; сырой `awg-quick up` при
  существующем интерфейсе отказывает ("already exists") и дублей (адресов, правил NAT) не плодит. Смена Endpoint идёт через
  `syncconf` без пересоздания, смена Address - `down` + `up` (правило NAT остаётся одно).
- `docker restart` шлюза: оба выхода поднимаются из `exits/*.conf` (рукопожатия, маршруты в таблицах, по одному правилу NAT),
  `ip rule` на месте. Битый `exits/gwx9.conf` контейнер не роняет: в логе "exit tunnel gwx9 failed to come up", остальные
  выходы подняты.
- `remove_exit(1)` убирает интерфейс, файл, маршрут таблицы 1001 и правило NAT, `gwx2` не затронут; повторный вызов
  ничего не ломает. Права: каталог `exits` 700, файлы 600.
- Расхождения с допущениями: таблица `1000 + слот` вместо `100 + слот` (см. выше); адрес внутреннего клиента первого клиента
  выхода - `10.8.1.1` (сервер выхода занимает `.0`), а не `.2`; `awg show interfaces` печатает интерфейсы в обратном порядке
  создания (порядок не используется).

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
- `read_importable()` (импорт, только чтение): конфиг и `clientsTable` → `ImportablePeer` (ключ, адрес, PSK,
  имя). Контейнера нет, он остановлен или не контейнер приложения, конфиг нечитаем — `CommandExecutionError`,
  а не «клиентов нет». Нечитаемая таблица — клиенты без имён.
- `clientsTable` — JSON в двух формах: список `{clientId, clientName}` или объект `{clientId: {clientName}}`; имя
  может лежать в `userData`. `parse_clients_table`: длиннее 1 МиБ (в символах) или не JSON → `{}`; из имени удаляются управляющие,
  невидимые и разделители строк (Unicode-категории Cc, Cs, Cf, Zl, Zp), длина ≤ 100.
- `discover_peers` (`awg_config.py`) берёт из серверного конфига `[Peer]`-ы: `PublicKey` (невалидный — пропуск) и
  `AllowedIPs`. Один IPv4 `/32` — адрес; иначе адреса нет и `supported=False` (несколько адресов, IPv6, не `/32`).
- Из серверного конфига берутся `ListenPort` и поля `[Peer]` выше. `PrivateKey` не попадает никуда. `PresharedKey` — только в `ImportedPeer` / `ImportablePeer` (импорт);
  в `Discovery` и отчёт поиска не попадает ни один из них.

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
