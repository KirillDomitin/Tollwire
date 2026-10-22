# infrastructure/vpn — подробная карта файлов

Полная версия таблицы из `src/infrastructure/vpn/CLAUDE.md`.

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
