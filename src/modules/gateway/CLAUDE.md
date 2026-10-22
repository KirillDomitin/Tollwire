# modules/gateway — шлюз: выходы, назначение, failover, «Россия напрямую»

Шлюз (протокол `AMNEZIA_GATEWAY`, контейнер `vpn-admin-gw`, см. `infrastructure/vpn/CLAUDE.md`) - клиент своих
«выходов»: зарубежных серверов панели с протоколом `AMNEZIA_WG`. Для каждого выхода панель создаёт на нём
«внутреннего клиента» обычным `create_client` и поднимает на шлюзе интерфейс `gwx<слот>` с конфигом этого клиента.
Какой клиент шлюза идёт через какой выход - назначение (24.3), отказы выходов - failover (24.5), российские адреса
напрямую - 24.4; ниже их инварианты, подробности - в `docs/gateway/`.

## Модель `gateway_exits`

`gateway_protocol_id` (FK `protocols.id` CASCADE - шлюз снимают, строки уходят), `exit_protocol_id` (FK RESTRICT -
пока выход подключён, его протокол и сервер не удалить), `slot` (1..200, UNIQUE в пределах шлюза), `weight` (1..100,
по умолчанию 1), `max_clients` (NULL - без предела), `state` (`ExitState`, VARCHAR: pending / peer_created / active /
draining / removing), `health` (ok / stale / no_handshake / down, NULL - не сверяли), `last_handshake_at`,
`checked_at`, внутренний клиент: `client_public_key`, `client_address`, `encrypted_config` (Fernet, NULL до
`peer_created`). UNIQUE `(gateway_protocol_id, exit_protocol_id)` - выход подключён к шлюзу один раз; один выход может
обслуживать несколько шлюзов. Слот - наименьший свободный (`free_slot`), при постановке протокол шлюза блокируется
`FOR UPDATE`, поэтому два одновременных POST не возьмут один слот. Имя интерфейса, файла и номер таблицы строятся
только из целого слота (`gateway_config.py`).

## API (`/api/v1/servers/{server_id}/gateway/exits`, `api/v1/router.py`)

| Метод | Право | Что |
|---|---|---|
| `GET` | `servers:read` | Выходы по слотам: `id, exit_server_id, exit_server_name, slot, state, health, last_handshake_at, checked_at, weight, max_clients`. Ключей, адреса и конфига внутреннего клиента нет. Нет шлюза - `[]`, нет сервера - 404. |
| `POST` | `servers:manage` | `{exit_server_id, weight=1, max_clients=null}` -> 202 `JobRead` (`gateway_add_exit`). Строка выхода создаётся сразу в `pending`. |
| `PATCH /{exit_id}` | `servers:manage` | `{weight?, max_clients?}` - только БД, без задачи; аудит `gateway_exit_updated` (только реальные изменения); `weight: null` - 422, `max_clients: null` снимает предел. |
| `DELETE /{exit_id}` | `servers:manage` | -> 202 (`gateway_remove_exit`); выход сразу в `draining`. |

Коды POST: 404 - нет сервера или сервера-выхода; 422 - выход = сам шлюз, вес/предел вне диапазона; 409 - нет SSH-учётки
(шлюза или выхода), на сервере нет шлюза, на выходе нет `AMNEZIA_WG` (или его порт неизвестен), выход сам шлюз, выход уже
добавлен, по выходу идёт незавершённая задача, слотов не осталось. Выход, у которого добавление не удалось (`pending`,
`peer_created`) и задачи нет, ставится повторно тем же POST (тот же слот, вес и предел обновляются). DELETE: 404 - нет
выхода у этого шлюза, 409 - по выходу идёт задача; повтор после упавшей задачи - снова DELETE.

## Задачи (`service.py`, воркер под `ServerLock` шлюза; выход блокируется внутри, порядок всегда шлюз -> выход)

`Job.params`: `exit_id`, `exit_server_id`, `slot`, `weight` (последние три - только для аудита; ключей нет).

- `gateway_add_exit` (`run_add`), две фазы с сохранённым состоянием, повтор идемпотентен:
  1. `pending`: под `ServerLock(выхода)` клиент `gw-<12 hex id строки>` на `AmneziaWGProvider` выхода; если клиент с таким
     именем уже есть (прошлая попытка создала, но commit не дошёл) - он снимается и создаётся заново; ключ, адрес и
     зашифрованный конфиг сохранены, `peer_created`, commit.
  2. `peer_created`: `apply_exits` на шлюзе (Endpoint = `host` сервера-выхода + порт его `AMNEZIA_WG`), `active`, commit.
  Занятый выход - `ServerBusyError`, воркер возвращает задачу в очередь (пауза 1 с); фаза 1 при этом уже сохранена.
  В конце клиентам шлюза без выхода назначается выход (`assign_unassigned`). Результат: `{exit_id, exit_server_id,
  slot, assigned}`.
- `gateway_remove_exit` (`run_remove`): `draining` -> `drain()` (перенос клиентов на другие выходы, 24.3; счётчики
  `moved` / `unassigned` в результате и в аудите) -> `removing` -> `remove_exit(slot)` на шлюзе -> `revoke_client` внутреннего клиента на выходе под
  `ServerLock(выхода)` -> строка удалена. Выход недоступен по SSH - туннель снят, строка удалена, `peer_revoked: false`;
  оставшегося `gw-*` снимет sync выхода. Строки уже нет - успех (`already_removed`).

**Подробности — читать перед работой над соответствующей частью:** назначение выходов клиентам —
`docs/gateway/assignments.md`; failover, ручной перенос, закрепление — `docs/gateway/failover.md`; «Россия
напрямую» — `docs/gateway/ru.md`. Что происходит на самом шлюзе (iptables, `ip rule`, скрипты) — `docs/vpn/gateway.md`.

## Назначение выходов (`assignment.py`, `balancer.py`) — инварианты

- Нет назначения → нет метки → kill-switch: клиент без интернета, но без утечки. БД (`gateway_assignments`,
  PK `credential_id`) — источник правды; на шлюз всегда уходит **полная** карта, собранная из БД заново, не инкрементом.
- Перед любым применением карты — идемпотентный `install()` шлюза (`_ready`); наличия цепочки `GW_ASSIGN` недостаточно.
- `pick_exit` — чистая функция: ACTIVE-выходы, health не `down`/`stale`, `assigned < max_clients`; минимум
  `(assigned + 1) / weight`, ничья — меньший слот.
- Порядок в сервисе: изменения БД → flush → карта на шлюзе → commit. Карта не применилась — ничего не сохранено.
- Всё под `ServerLock` шлюза. В аудите и журнале нет адресов и ключей.

## Failover (`failover.py`) — инварианты

- Здоровье выхода — только по туннелю (интерфейс `gwxN` + рукопожатие ≤ 180 с), не по `servers.health_status`.
- Отказ — 2 плохие проверки подряд (`next_health`, гистерезис); одна плохая — выход не получает новых клиентов.
- Плохи **все** выходы сразу — счётчики не растут, переносов нет, одно событие `gateway_all_exits_down`.
- Счётчик ведёт только правило `gateway_watch` (раз в 60 с, шлюзы параллельно, таймаут 20 с на шлюз); ручная проверка
  гистерезис не обходит.
- `GATEWAY_APPLY` пересчитывает план по свежей БД; пустой план — шлюз не трогается. `plan_failover` детерминирован,
  повтор по применённому состоянию пуст. Закреплённые возвращаются через `return_after` (600 с), не больше 5 за проход.
- Ручной `PATCH .../assignments/{credential_id}` — синхронно под `ServerLock`, сбой → 502 без деталей.

## «Россия напрямую» (`ru.py`) — инварианты

- На шлюзе правило 100 (таблицы 200/201 по очереди) стоит раньше меток выходов (1000) и kill-switch (9000).
- Список «применён», только если совпали **и** отпечаток `sha256`, **и** число маршрутов.
- Эффективный список вычитает адреса самого шлюза и его выходов: исключение не должно ломать туннели и SSH.
- Проверка health `ru` есть только при активном списке. Задача `gateway_apply_ru` — одна на шлюз, без дублей.

## Аудит

`gateway_exit_add_started/completed/failed`, `gateway_exit_remove_started/completed/failed` (через таблицы
`_STARTED/_COMPLETED/_FAILED` в `jobs/service.py`, сущность `server` = шлюз) и `gateway_exit_updated`. Meta: `job_id`,
`exit_id`, `exit_server_id`, `slot`, `weight` (у add), `error`; для PATCH - `changes`. Ключей и конфигов нет.

## Защиты и связи

- Нельзя удалить сервер-выход и снять с него `AMNEZIA_WG`, пока на него ссылается `gateway_exits`
  (`LiveCredentialExistsError` -> 409; в `ServerService.schedule/delete` и повторно в `protocol_uninstall` воркера).
- Удаление сервера-шлюза и снятие протокола шлюза каскадом удаляют его выходы; клиенты `gw-*` на выходах остаются, их
  снимает sync выхода.
- Sync выхода (`servers/sync.py`): `gw-*` с записью в состоянии peer_created / active / draining - живой (потерянный
  возвращается `restore_client` по сохранённому конфигу); `gw-*` без такой записи (в том числе у `removing`) - сирота и
  снимается; прочее не трогается.
- Sync шлюза: отчёт `exits` и `issues` ("Exit gwxN is down/missing on the gateway" для `active` без интерфейса, "Unknown
  exit tunnel" для туннеля без записи), обновление `health` / `last_handshake_at` / `checked_at`. Автодействий нет
  (failover ведёт правило `gateway_watch`, 24.5). Health-задача шлюза обновляет те же поля.

## Известные ограничения

- Снять выход при навсегда недоступном шлюзе нельзя (задача снятия падает на SSH к шлюзу); такой выход уходит вместе с
  удалением сервера-шлюза (строки выходов удаляются каскадом, внутренних клиентов `gw-*` снимет sync выхода). Принудительного
  `?force=true` нет.
- Health-задача шлюза опрашивает туннели второй раз (`exit_status` для БД поверх проверки провайдера) - лишние две команды на
  выход; осознанная неоптимальность.
- Имя внутреннего клиента - строго `gw-` + 12 hex (`is_exit_client_name`): sync снимает только такие; чужие `gw-*` (например,
  `gw-office` из приложения AmneziaVPN) попадают в `foreign` и не трогаются.
- Сбой чтения туннелей (`awg show interfaces`) во время sync шлюза не теряет отчёт по клиентам: в `issues` -
  "Failed to list exit tunnels on the gateway", `exits` пуст, health строк не меняется.
- POST и DELETE берут блокировку строки протокола шлюза (`lock_gateway`): ADD и REMOVE одного выхода не ставятся вместе.

## Файлы

`service.py` (`GatewayExitService`, задачи add/remove), `assignment.py` (`GatewayAssignmentService`), `balancer.py`
(`pick_exit`, `next_health`, `plan_failover` — чистые функции), `failover.py` (`GatewayFailoverService`, `run_apply`),
`ru.py` (`GatewayRuService`), `tunnels.py` (`exit_tunnel` — `ExitTunnel` из строки выхода), `exit_health.py` (чистая
сверка для sync/health), `naming.py` (`gw-` + `exit_client_name`), `models/`, `repository.py`, `schemas.py`, `api/v1/router.py`.
