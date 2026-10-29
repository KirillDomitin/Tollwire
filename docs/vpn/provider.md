# Справочник (вынесено из CLAUDE.md)

Описательные части: карта файлов, модели, API, операции. Инварианты и грабли остались в CLAUDE.md.

## Формат vpn:// и QR

- `vpn://` + base64url без `=` от (4 байта длины big-endian + zlib(JSON)) — как `qCompress` в AmneziaVPN.
  JSON: `containers[0]` = `amnezia-awg2` с параметрами обфускации и `last_config`; `description` = имя сервера в
  панели (его показывает приложение).
- QR — серия кусков по 850 байт: заголовок `>hBBI` (magic 1984, число кусков, номер, длина) + байты, base64url,
  коррекция ошибок LOW. Один плотный QR камера не читала — только серия.

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
