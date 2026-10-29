# Справочник (вынесено из CLAUDE.md)

Описательные части: карта файлов, модели, API, операции. Инварианты и грабли остались в CLAUDE.md.

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

## Аудит

`server_created/updated/deleted`, `server_credentials_set/deleted`, `server_host_key_pinned/reset/mismatch`,
`server_install_*`, `server_sync_*`, `server_discover_*`, `server_ssh_key_install_*` (started/completed/failed), `protocol_install_*`,
`protocol_remove_*`, `gateway_exit_*` (см. `modules/gateway/CLAUDE.md`). Meta: `panel_key_left_on_server: <fingerprint>` при удалении учётки или сервера с ключом панели. У `server_discover_*` meta — счётчики `protocols` и `clients`
(без ключей и адресов).
