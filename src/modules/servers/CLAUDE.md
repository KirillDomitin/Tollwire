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
| `service.py` | `ServerService` (CRUD, `schedule(job_type)` — поставить серверную задачу), `CredentialService`, `ServerConnectionService` (`create_ssh_client` с опц. `private_key`), `provider_class(ProtocolType)`. `panel_key_warning()` — предупреждение о ключе панели при удалении учётки/сервера. |
| `ssh_key.py` | `ServerSshKeyService`: `schedule_install(server_id, credential)` — поставить задачу; `install(server_id)` — из воркера, генерирует ключ, ставит его, переходит на вход, меняет учётку. |
| `host_key.py` | `ServerHostKeyService`: scan (без входа), pin, reset, `learned`, `mismatch` (аудит + статус ERROR). Пишет в своей транзакции. |
| `sync.py` | `ServerSyncService` — сверка сервера с БД под ServerLock, отчёт в `Job.result`. |
| `health.py` | `ServerHealthService` — только чтение, без ServerLock; отчёт в `Job.result` + сводка на сервере. |
| `maintenance.py` | Плановые health/sync для планировщика. |
| `reachability.py` | `mark_unreachable` / `mark_reachable`. |

## API (`/api/v1/servers`)

CRUD; `PUT/GET/DELETE /{id}/credentials`; `POST /{id}/test-connection`; `POST /{id}/host-key/scan`,
`PUT/DELETE /{id}/host-key`; задачи (202 + `JobRead`): `POST /{id}/install`, `POST /{id}/ssh-key`,
`POST/DELETE /{id}/protocol`, `POST /{id}/sync`, `POST /{id}/health`; `GET /{id}/protocols`. Права —
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
- Sync чинит по БД: живых клиентов восстанавливает из сохранённого конфига, отозванных и «сирот» с именем
  `grant-*` удаляет, чужие peer'ы — только в отчёт. Меняет статус сервера (OFFLINE/READY).
- Health: SSH, Docker, контейнер, протокол, NAT, задержка до интернета. SSH недоступен → задача SUCCESS со
  статусом down.

## Известные проблемы

- Sync удаляет всех «сирот» `grant-*`: если один сервер подключён к двум панелям (dev и prod), каждая удаляет
  клиентов другой. Решение-кандидат — метка панели в имени peer'а.
- Health при незакреплённом ключе хоста пишет `down`, хотя сервер жив (не различает «недоступен» и «нельзя войти
  по правилам»).

## Аудит

`server_created/updated/deleted`, `server_credentials_set/deleted`, `server_host_key_pinned/reset/mismatch`,
`server_install_*`, `server_sync_*`, `server_ssh_key_install_*` (started/completed/failed), `protocol_install_*`,
`protocol_remove_*`. Meta: `panel_key_left_on_server: <fingerprint>` при удалении учётки или сервера с ключом панели.
