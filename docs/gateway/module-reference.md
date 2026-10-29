# Справочник (вынесено из CLAUDE.md)

Описательные части: карта файлов, модели, API, операции. Инварианты и грабли остались в CLAUDE.md.

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

## Аудит

`gateway_exit_add_started/completed/failed`, `gateway_exit_remove_started/completed/failed` (через таблицы
`_STARTED/_COMPLETED/_FAILED` в `jobs/service.py`, сущность `server` = шлюз) и `gateway_exit_updated`. Meta: `job_id`,
`exit_id`, `exit_server_id`, `slot`, `weight` (у add), `error`; для PATCH - `changes`. Ключей и конфигов нет.

## Файлы

`service.py` (`GatewayExitService`, задачи add/remove), `assignment.py` (`GatewayAssignmentService`), `balancer.py`
(`pick_exit`, `next_health`, `plan_failover` — чистые функции), `failover.py` (`GatewayFailoverService`, `run_apply`),
`ru.py` (`GatewayRuService`), `tunnels.py` (`exit_tunnel` — `ExitTunnel` из строки выхода), `exit_health.py` (чистая
сверка для sync/health), `naming.py` (`gw-` + `exit_client_name`), `models/`, `repository.py`, `schemas.py`, `api/v1/router.py`.
