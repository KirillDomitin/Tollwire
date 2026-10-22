# Чужая установка приложения AmneziaVPN: чтение, отзыв, протокол записи

Перенесено из `src/infrastructure/vpn/CLAUDE.md` без изменений текста; там остались только инварианты.

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
