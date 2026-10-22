# Серверы: шлюз, его выходы и «Россия напрямую»

Перенесено из `src/modules/servers/CLAUDE.md` без изменений текста; там остались инварианты.

## Шлюз (`amnezia_gateway`, этап 24.1)

- Установка и снятие - те же `POST/DELETE /{id}/protocol` с телом `{"type": "amnezia_gateway"}` или `?type=amnezia_gateway`
  (`requested_protocol` в роутере: пустое тело `{}` тип не задаёт). Тип не обычный
  кладётся в `Job.params` (`{"protocol_type": "amnezia_gateway"}`); у обычного протокола `params` нет, задачи без
  `params` воркер читает как `amnezia_wg`. `ServerService.schedule(server_id, job_type, protocol_type)`.
- Обычный протокол и шлюз живут на одном сервере независимо: разные контейнеры, порты и подсети; `protocols` уникальна
  по `(server_id, type)`. Живые клиенты блокируют снятие (409) так: шлюз - только клиенты шлюза; обычный протокол -
  клиенты всех остальных типов, кроме шлюза (прежнее поведение, в том числе для импортированных); удаление сервера -
  любые.
- Sync и health перебирают все `ProtocolType`: у сервера без шлюза запись для него не создаётся (нет контейнера и
  записи в БД - в отчёт не попадает). Шлюз без выходов даёт проверку `exits` = false и сервер DEGRADED. Поиск Amnezia
  (`discover`) шлюз игнорирует. Мониторинг суммирует клиентов всех протоколов сервера.

### Выходы шлюза (этап 24.2)

Подробно - `modules/gateway/CLAUDE.md`. Здесь изменилось поведение сервера:

- `ServerService.delete` и `schedule(REMOVE_PROTOCOL, AMNEZIA_WG)` отказывают (409, `LiveCredentialExistsError`), пока
  сервер - выход какого-либо шлюза (`GatewayExitRepository.exists_for_exit_server`); воркер проверяет то же перед
  снятием контейнера (`protocol_uninstall`). Удаление сервера-шлюза и снятие протокола шлюза удаляют его выходы каскадом.
- Sync (`sync.py`): на `AMNEZIA_WG` внутренние клиенты шлюза (имя строго `gw-` + 12 hex, иные `gw-*` - чужие, в `foreign`) с записью в `gateway_exits` (peer_created / active /
  draining) живые - при потере возвращаются по сохранённому конфигу; без такой записи - сироты и снимаются. У шлюза
  в отчёт протокола добавляется `exits` (по слоту: `exit_id, slot, state, interface_up, health`), а в `issues` -
  "Exit gwxN is down/missing on the gateway" для `active` без интерфейса и "Unknown exit tunnel" для чужого туннеля; сбой чтения туннелей - issue "Failed to list exit tunnels", отчёт по клиентам сохраняется;
  обновляются `health` / `last_handshake_at` / `checked_at` строк. Действий нет (failover ведёт правило `gateway_watch`, см. `modules/gateway/CLAUDE.md`).
- Sync шлюза сначала вызывает `install()` шлюза (дочинивает `start.sh`, правила и sysctl; пересоздание контейнера - в аудит и
  отчёт), потом сверяет назначения выходов клиентам (`GatewayAssignmentService.reconcile`, отчёт `assignments`:
  `assigned`, `newly_assigned`, `reapplied`): карта в цепочке `GW_ASSIGN` и файл `assign.rules` приводятся к БД, клиенты без
  выхода получают его (`sync`), туннели выходов с конфигом прежней версии пересоздаются. Сбой сверки - issue, sync не падает.
- Health (`health.py`): проверка `exits` шлюза - хотя бы один туннель с интерфейсом и рукопожатием не старше 180 с; те же
  поля строк выходов обновляются и здесь (если контейнер шлюза не отвечает - остаются прежними).

### Россия напрямую (этап 24.4)

Подробно - `modules/gateway/CLAUDE.md` и `modules/geoip/CLAUDE.md`. Sync шлюза добавляет в отчёт протокола `ru`
(`GatewayRuService.ensure`: есть активный список, а маршрутов на шлюзе не столько, сколько в эффективном списке, - применить;
нет списка - `{active_list: false}`), health - проверку `ru` (только когда есть активный список), установка шлюза в воркере -
`ensure_server`. Сбой применения - issue, sync не падает.
