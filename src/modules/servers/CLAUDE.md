# modules/servers — VPN-серверы

Сервер — Linux-машина, которой панель управляет по SSH. Здесь: запись сервера, SSH-учётка, ключ хоста,
подключение, установка (готовность Docker), протокол, сверка (sync) и проверка состояния (health).

## Модели

- `vpn_servers`: name (unique), host, port, description, `status` (`ServerStatus`), `health_status`
  (ok/degraded/down) + `last_health_at`, `ssh_host_key` + `ssh_host_key_fingerprint` (закреплённый ключ).
- `server_credentials` (1:1, `server_credentials_server_id_key`): username, `auth_type` (password/ssh_key),
  секрет — **только** Fernet (`encrypt_secret`). Наружу секрет не отдаётся никогда.

`ServerStatus` — жизненный цикл записи в панели: NEW → INSTALLING → READY; OFFLINE (SSH недоступен), ERROR.
Это не то же самое, что `ProviderState` провайдера (NOT_INSTALLED/STOPPED/RUNNING — что физически на сервере);
переводит одно в другое сервис. Общие правила «стал недоступен / снова доступен» — `reachability.py`.

## Файлы

| Файл | Что внутри |
|---|---|
| `service.py` | `ServerService` (CRUD, `schedule(job_type)` — поставить серверную задачу), `CredentialService`, `ServerConnectionService` (`create_ssh_client`, `test_connection`, `server_install`, `protocol_install/uninstall`), `provider_class(ProtocolType)` — фабрика провайдеров. |
| `host_key.py` | `ServerHostKeyService`: scan (без входа), pin, reset, `learned`, `mismatch` (аудит + статус ERROR). Пишет в своей транзакции. |
| `sync.py` | `ServerSyncService` — сверка сервера с БД под ServerLock, отчёт в `Job.result`. |
| `health.py` | `ServerHealthService` — только чтение, без ServerLock; отчёт в `Job.result` + сводка на сервере. |
| `maintenance.py` | Плановые health/sync для планировщика. |
| `reachability.py` | `mark_unreachable` / `mark_reachable`. |

## API (`/api/v1/servers`)

CRUD; `PUT/GET/DELETE /{id}/credentials`; `POST /{id}/test-connection`; `POST /{id}/host-key/scan`,
`PUT/DELETE /{id}/host-key`; задачи (202 + `JobRead`): `POST /{id}/install`, `POST/DELETE /{id}/protocol`,
`POST /{id}/sync`, `POST /{id}/health`; `GET /{id}/protocols`. Права — см. `modules/auth/CLAUDE.md`.

## Правила

- Порядок подготовки: ключ хоста (scan → pin) → SSH-учётка → установка (Docker) → протокол.
- Без учётки задача не ставится (`CredentialNotFound` → 409).
- Смена `host` — другая машина: сбрасываются закреплённый ключ и SSH-учётка (иначе пароль старого сервера ушёл
  бы на новый адрес).
- Удалить сервер или снять протокол с живыми VPN-клиентами нельзя (`LiveCredentialExistsError` → 409): каскад в
  БД удалил бы записи, а peer'ы остались бы на сервере.
- До SSH-вызова — `session.rollback()`/commit, чтобы не держать транзакцию на время сети. Нужные поля ORM-объектов
  копировать **до** rollback (после него объекты истекают → `DetachedInstanceError`/`MissingGreenlet`).
- Sync чинит по БД: живых клиентов восстанавливает из сохранённого конфига, отозванных и «сирот» с именем
  `grant-*` удаляет, чужие peer'ы — только в отчёт. Меняет статус сервера (OFFLINE/READY).
- Health: SSH, Docker, контейнер, протокол, NAT, задержка до интернета. SSH недоступен → задача SUCCESS со
  статусом down. Известный пробел: незакреплённый ключ тоже даёт down, хотя сервер жив.

## Аудит

`server_created/updated/deleted`, `server_credentials_set/deleted`, `server_host_key_pinned/reset/mismatch`,
`server_install_*`, `server_sync_*`, `protocol_install_*`, `protocol_remove_*`.
