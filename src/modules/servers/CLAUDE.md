# modules/servers — VPN-серверы

Сервер — Linux-машина, которой панель управляет по SSH. Здесь: запись сервера, SSH-учётка, ключ хоста,
подключение, установка (готовность Docker), протокол, сверка (sync) и проверка состояния (health).

## Модели

- `vpn_servers`: name (unique), host, port, description, `status` (`ServerStatus`), `health_status`
  (ok/degraded/down) + `last_health_at`, `ssh_host_key` + `ssh_host_key_fingerprint` (закреплённый ключ).
- `server_credentials` (1:1, `server_credentials_server_id_key`): username, `auth_type` (password/ssh_key),
  секрет — **только** Fernet (`encrypt_secret`). Наружу секрет не отдаётся никогда. Поля для ключа панели:
  `managed_key` (bool, панель сгенерировала), `public_key` (OpenSSH), `key_fingerprint` (SHA256:…).

`ServerStatus` — жизненный цикл записи в панели: NEW → INSTALLING → READY; OFFLINE (SSH недоступен), ERROR.
Это не то же самое, что `ProviderState` провайдера (NOT_INSTALLED/STOPPED/RUNNING — что физически на сервере);
переводит одно в другое сервис. Общие правила «стал недоступен / снова доступен» — `reachability.py`.

## Файлы

| Файл | Что внутри |
|---|---|
| `service.py` | `ServerService` (CRUD, `schedule(job_type)` — поставить серверную задачу), `CredentialService`, `ServerConnectionService` (`create_ssh_client` с опц. `private_key`), `provider_class(ProtocolType)` (`AMNEZIA_APP_AWG*` — классы приложения). `panel_key_warning()` — предупреждение о ключе панели при удалении учётки/сервера. |
| `ssh_key.py` | `ServerSshKeyService`: `schedule_install(server_id, credential)` — поставить задачу; `install(server_id)` — из воркера, генерирует ключ, ставит его, переходит на вход, меняет учётку. |
| `host_key.py` | `ServerHostKeyService`: scan (без входа), pin, reset, `learned`, `mismatch` (аудит + статус ERROR). Пишет в своей транзакции. |
| `sync.py` | `ServerSyncService` — сверка сервера с БД под ServerLock, отчёт в `Job.result`. |
| `health.py` | `ServerHealthService` — только чтение, без ServerLock; отчёт в `Job.result` + сводка на сервере. |
| `discovery.py` | `ServerDiscoveryService` — поиск уже установленной AmneziaVPN (только чтение): `discover(server_id)` (задача `DISCOVER_VPN`), `latest(server_id)` (отчёт и статусы клиентов). |
| `maintenance.py` | Плановые health/sync для планировщика. |
| `reachability.py` | `mark_unreachable` / `mark_reachable`. |

## API (`/api/v1/servers`)

CRUD; `PUT/GET/DELETE /{id}/credentials`; `POST /{id}/test-connection`; `POST /{id}/host-key/scan`,
`PUT/DELETE /{id}/host-key`; задачи (202 + `JobRead`): `POST /{id}/install`, `POST /{id}/ssh-key`,
`POST/DELETE /{id}/protocol` (тип: необязательное тело `{"type": "amnezia_wg" | "amnezia_gateway"}` и/или query `?type=...` - у DELETE тело нередко отбрасывает прокси или клиент, поэтому query надёжнее; оба и разные - 422; ни одного - `amnezia_wg`; приложение-типы и неизвестные - 422), `POST /{id}/sync`, `POST /{id}/health`, `POST /{id}/discover`, `POST /{id}/import` (`servers:manage`, тело — см. «Импорт клиентов»); `GET /{id}/protocols`; `GET /{id}/discovery` (отчёт поиска, не задача; 404 — сервера нет). `discover` — право
`servers:check`, `discovery` — `servers:read`. Права —
см. `modules/auth/CLAUDE.md`.

## Правила

- Порядок подготовки: ключ хоста (scan → pin) → SSH-учётка → установка (Docker) → протокол.
- Без учётки задача не ставится (`CredentialNotFound` → 409).
- Ключ панели: `POST /{id}/ssh-key` требует закреплённый ключ хоста; повторный вызов ставит новый ключ вместо
  прежнего (старые удаляются по метке `vpn-panel:<server_id>` после успеха). Параметры тела `{username?, password?}`
  — оба или ни одного; сохраняются как учётка перед постановкой задачи. Ключ панели остаётся на сервере при
  удалении/смене host/смене учётки (снять его по SSH не получится) — в аудите `panel_key_left_on_server`.
- Смена `host` — другая машина: сбрасываются закреплённый ключ и SSH-учётка (иначе пароль старого сервера ушёл
  бы на новый адрес).
- Удалить сервер или снять протокол с живыми VPN-клиентами нельзя (`LiveCredentialExistsError` → 409): каскад в
  БД удалил бы записи, а peer'ы остались бы на сервере.
- До SSH-вызова — `session.rollback()`/commit, чтобы не держать транзакцию на время сети. Нужные поля ORM-объектов
  копировать **до** rollback (после него объекты истекают → `DetachedInstanceError`/`MissingGreenlet`).
- В сервисе, который вызывает воркер, — только commit: сессия общая с `Job`, rollback «истечёт» его, и воркер упадёт
  на записи итога (`expire_on_commit=False` бережёт объекты при commit).
- Ключ панели (`ssh_key.py`): записать в authorized_keys → войти новым ключом отдельным соединением → под
  `FOR UPDATE` сверить `updated_at` учётки и заменить её → снять прежние ключи по метке. Сбой до замены снимает
  новый ключ (итог `rolled_back` в аудите); если замена в БД всё-таки прошла (потерян ответ commit) — ключ остаётся.
- Sync чинит по БД: живых клиентов восстанавливает из сохранённого конфига (импортированных — `restore_peer` по PSK и
  адресу; без PSK — в `issues`, peer не возвращается), отозванных и «сирот» с именем `grant-*` удаляет, чужие
  peer'ы — только в отчёт. Меняет статус сервера (OFFLINE/READY).
- Приложение AmneziaVPN (`APP_PROTOCOLS`): «сирот» не удаляет (имена клиентов приложения не доверенные, такой peer —
  в `foreign`). Отзыв по credential'ам БД (`revoked_keys`, `stale`) пишет в контейнер приложения (`revoke_client`).
  Восстановление: импортированные credential'ы — `restore_peer`; выданный конфиг (в норме таких нет) —
  `restore_client` → `UnsupportedOperationError`: клиент попадает в `issues`, sync не падает. Строку APP-протокола
  создаёт только импорт (`ClientImportService`), `install` для них не поддержан.
- Health: SSH, Docker, контейнер, протокол, NAT, задержка до интернета. SSH недоступен → задача SUCCESS со
  статусом down.

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
  обновляются `health` / `last_handshake_at` / `checked_at` строк. Действий нет (failover - 24.5).
- Health (`health.py`): проверка `exits` шлюза - хотя бы один туннель с интерфейсом и рукопожатием не старше 180 с; те же
  поля строк выходов обновляются и здесь (если контейнер шлюза не отвечает - остаются прежними).

## Поиск установки AmneziaVPN (`discovery.py`)

Поиск только читает сервер. Найденное приложение панель не ставит, не удаляет и не меняет его клиентов. Перенос
клиентов в БД панели — отдельная задача импорта (раздел «Импорт клиентов» ниже); сервер при нём не меняется.

- `POST /{id}/discover` → задача `DISCOVER_VPN` (без ServerLock). `discover()` спрашивает провайдер каждого
  `ProtocolType`; отвечают только классы приложения (`AmneziaWGProvider.discover()` — None, в отчёт не входит).
  Результат: `{"protocols": [{type, container, state, image, port, clients: [{public_key, address, name,
  latest_handshake, supported}]}]}`. В БД ничего не пишется.
- `GET /{id}/discovery` — отчёт последней успешной задачи `DISCOVER_VPN` или `INSTALL_SERVER` с непустым result;
  `checked_at` — время её окончания. Отчёта нет → `checked_at: null`, `protocols: []`.
- Статус клиента в `GET /discovery`: `unsupported` (`supported=false`) → `in_panel` (ключ — живой `VPNCredential`
  этого сервера) → `revoked_in_panel` (ключ — отозванный) → `new`.
- Автопоиск после `INSTALL_SERVER`: тот же поиск внутри задачи установки, его отчёт становится её `result`. Сбой
  поиска откатывает сессию, пишет warning в лог; установка остаётся успешной (отчёта в ней нет).
- Sync (`_sync_protocol`): для `APP_PROTOCOLS` без записи в БД и без контейнера в отчёт ничего не попадает.
  Найденный контейнер (в том числе остановленный) — запись с `in_db: false` и issue «Installed on server but not
  tracked in DB». Клиенты не трогаются.
- Health (`_check_protocol`): у app-типа без записи в БД проверка `tracked_in_db` = ok, текст «installed by the
  AmneziaVPN app, not managed». Сама установка приложения сервер в DEGRADED не переводит; остановленный контейнер
  или нечитаемый конфиг приложения переводят (их проверки ok=false). Контейнера нет — в отчёт не попадает.

## Импорт клиентов AmneziaVPN (`POST /{id}/import`)

Перенос клиентов установленной AmneziaVPN в БД панели делает `ClientImportService` (`modules/access/import_service.py`,
логика — в `modules/access/CLAUDE.md`). Сервер не меняется.

- Тело: `protocol_type` (только протокол приложения), `valid_until` (с часовым поясом, в будущем), `clients`
  (1–250, ключи не повторяются). У клиента: `public_key`, `username` и либо `user_id` существующего пользователя,
  либо необязательный `email` для нового. Лишние поля (в том числе PSK) игнорируются: PSK берётся с сервера.
- Ответ — 202 с `JobRead`; итог — в `result` задачи `IMPORT_CLIENTS` (формат — `modules/jobs/CLAUDE.md`).
- 422 — пустой или повторяющийся список, больше 250 клиентов, не протокол приложения, `valid_until` не в будущем,
  `user_id` и `email` вместе, неверный ключ или имя.
- 404 — сервера нет, `user_id` не найден. 409 — нет SSH-учётки; ключа нет в последнем отчёте поиска этого
  протокола (`GET /{id}/discovery`) или клиент не поддерживается; на сервере уже идёт импорт.
- `DELETE /{id}/protocol` при живых импортированных клиентах → 409 (`LiveCredentialExistsError`), как и для
  обычных.

## Известные проблемы

- Sync удаляет всех «сирот» `grant-*` (кроме APP-протоколов): если один сервер подключён к двум панелям (dev и prod), каждая удаляет
  клиентов другой. Решение-кандидат — метка панели в имени peer'а.
- Health при незакреплённом ключе хоста пишет `down`, хотя сервер жив (не различает «недоступен» и «нельзя войти
  по правилам»).
- Импортированный клиент без PSK (в конфиге сервера нет `PresharedKey`) sync не возвращает на сервер: без
  `encrypted_psk` peer не восстанавливается, и в `issues` каждый раз попадает запись.

## Аудит

`server_created/updated/deleted`, `server_credentials_set/deleted`, `server_host_key_pinned/reset/mismatch`,
`server_install_*`, `server_sync_*`, `server_discover_*`, `server_ssh_key_install_*` (started/completed/failed), `protocol_install_*`,
`protocol_remove_*`, `gateway_exit_*` (см. `modules/gateway/CLAUDE.md`). Meta: `panel_key_left_on_server: <fingerprint>` при удалении учётки или сервера с ключом панели. У `server_discover_*` meta — счётчики `protocols` и `clients`
(без ключей и адресов).
