# modules/access — подписки, доступы, VPN-клиенты

Цепочка: `VPNUser` → `Subscription` (срок) → `AccessGrant` (сервер + протокол) → `VPNCredential` (клиент на
сервере и его зашифрованный конфиг).

Срок доступа — у `Subscription`, а не у гранта: одна подписка на несколько серверов истекает разом; у гранта свой
статус для частичного отзыва одного сервера. Это точка расширения под будущие тарифы и оплату.

## Модели (`models/`)

- `subscriptions`: user_id (CASCADE), `status` (`AccessGrantStatus`), `valid_from`, `valid_until`, `revoked_at`.
  Одна незавершённая (PENDING/ACTIVE) подписка на пользователя — частичный уникальный индекс.
- `access_grants`: subscription_id, server_id, protocol_id (все CASCADE), `status`, `revoked_at`. Своего срока
  нет; `user_id` не дублируется (берётся через подписку). Частичный уникальный индекс (subscription_id,
  protocol_id) для незавершённых.
- `vpn_credentials`: access_grant_id (CASCADE), `encrypted_config` (Fernet; NULL у импортированного),
  `client_address`, `client_public_key`, `revoked_at`, `source` (`CredentialSource`: `ISSUED` — клиента создала
  панель, `IMPORTED` — подхвачен из AmneziaVPN), `encrypted_psk` (Fernet; PSK импортированного клиента, NULL —
  PSK не было), `external_name` (имя из `clientsTable`, для показа). История перевыпусков + не больше одного
  живого на грант (частичный индекс `WHERE revoked_at IS NULL`). CHECK `ck_vpn_credentials_issued_has_config`:
  у `ISSUED` конфиг обязателен. Приватного ключа импортированного клиента у панели нет, поэтому конфига и
  share-ссылок у него нет.
- `AccessGrantStatus`: PENDING → ACTIVE → EXPIRED / REVOKED (два последних — терминальные).

## Правила (`service.py`)

- `inactive_reason(grant, subscription, now)` — единое правило «доступ живой» (статусы + даты); его используют
  выдача, показ конфига и sync.
- Подписка создаётся PENDING; планировщик (правило `access`) переводит её в ACTIVE, когда наступил `valid_from`,
  и в EXPIRED после `valid_until`. Грант становится ACTIVE, когда для него реально создан клиент (provision).
  REVOKED главнее истечения. Отзыв и истечение подписки каскадом переводят незавершённые гранты.
- Грант: на вход только `protocol_id`, сервер выводится из протокола. Конфликт — уже есть живой грант того же
  пользователя на тот же сервер и протокол. Выдача до `valid_from` запрещена (409).
- Отзыв и истечение гранта/подписки ставят задачи `REVOKE_ACCESS` (`schedule_revoke_for_grants`) — клиент
  удаляется с сервера, а не только в БД.
- Удалить грант, подписку или пользователя с живым клиентом нельзя (409) — сначала отзыв.
- Всё идемпотентно: повторный revoke не меняет `revoked_at` и не падает.

## VPN-клиент (`VPNCredentialService`)

- `schedule_issue` → Job `CREATE_USER` (202); 409, если уже есть живой credential или незавершённая выдача.
- `provision` (в воркере, под ServerLock): `provider.create_client(client_name(grant_id))` → имя `grant-<12 hex>`,
  грант перечитывается `FOR UPDATE` (гонка «отозвали, пока выдача шла»), шифрованный конфиг в БД, грант → ACTIVE.
- `deprovision` / `schedule_revoke` → `REVOKE_ACCESS`; повторный отзыв уже отозванного → 200 с credential.
- `reveal_config` — единственное место расшифровки: `config` (.conf), `share_link` (`vpn://`), `share_qr`
  (серия QR); имя сервера из панели → `description`. Ответ `Cache-Control: no-store`, аудит `config_viewed`,
  право `config:reveal` (у viewer нет).
- У импортированного (`encrypted_config` NULL) `reveal_config` → `ConfigUnavailableError` (409) до записи
  в аудит.

## Импорт клиентов AmneziaVPN (`import_service.py`)

Клиенты, созданные в приложении AmneziaVPN, переносятся в БД панели: пользователь (существующий или новый),
подписка, грант на протокол приложения и credential с `source = IMPORTED`. Сервер не меняется: peer уже на нём.
Срок доступа задаёт админ (`valid_until`). Конфига у такого клиента нет — приватный ключ остался в приложении.

- `schedule(server_id, data)` (из `POST /servers/{id}/import`) — проверки по БД и задача `IMPORT_CLIENTS`
  с `params`: сервера нет — 404, SSH-учётки нет — 409; `user_id` не найден — 404; ключа нет в **последнем**
  отчёте поиска этого протокола или клиент не `supported` — `ImportNotAllowedError` (409); на сервере уже
  есть незавершённый импорт — 409.
- `run(job)` — в воркере под `ServerLock`: заново читает peer'ы с PSK (`read_importable`) и порт; строки
  протокола приложения нет — создаёт её.
- Исход по клиенту: `imported` — создан; `already` — в протоколе уже есть живой credential с этим ключом;
  `gone` — ключа нет на сервере; `unsupported` — у peer не один IPv4 `/32`; `conflict` — с причиной в `reason`.
- `conflict`: email занят другим пользователем; у пользователя уже есть незавершённый грант на этот сервер и
  протокол (`find_active_conflict`) — одно устройство = один живой грант на протокол, второе устройство того
  же пользователя сюда попадает; подписка пользователя не действует сейчас; `valid_until` в прошлом, когда
  подписки нет; ошибка БД (уникальность, гонка с API) — общий текст, без параметров INSERT.
- Пользователь: `user_id` — существующий; иначе новый из `username` и необязательного `email` (без email —
  NULL).
- Подписка: живая подписка пользователя (PENDING или ACTIVE) переиспользуется, только если действует сейчас;
  ещё не начавшаяся не подходит (sync снял бы peer). Иначе создаётся ACTIVE на срок из запроса, `valid_from` —
  текущий момент.
- Каждый клиент — в savepoint (`begin_nested`): сбой одного не откатывает остальных. Commit делает воркер
  вместе со статусом задачи: упавшая задача не оставляет частичного импорта.
- Аудит: `credential_imported` на клиента; `vpn_user_created` с `{source: import}` для нового пользователя.
- Отзыв и истечение импортированного клиента — как обычного: `REVOKE_ACCESS` убирает peer через `revoke_client`.

## API

`/vpn-users/{id}/subscriptions` (GET/POST) · `/subscriptions/{id}` (GET/PATCH/DELETE) · `/subscriptions/{id}/revoke`
· `/subscriptions/{id}/grants` (GET/POST) · `/access/{id}` (DELETE) · `/access/{id}/revoke` ·
`/access/{id}/credentials` (GET, POST — выдать клиента) · `/credentials/{id}` · `/credentials/{id}/config` (POST)
· `/credentials/{id}/revoke`. PATCH гранта удалён намеренно. Импорт клиентов — `POST /servers/{id}/import`
(см. `modules/servers/CLAUDE.md`); у импортированного `/credentials/{id}/config` → 409.

## Аудит

`subscription_*`, `access_granted/updated/revoked/expired/deleted`, `credential_issued/issue_failed/revoked/
revoke_failed`, `config_viewed`, `credential_imported`, `clients_import_started/completed/failed` (пишет
`JobService`, см. `modules/jobs/CLAUDE.md`). Секреты в метаданные аудита не пишутся.

Для журнала (фильтры, ссылки на объекты) в `meta` есть id: `subscription_updated` — `user_id`;
`access_revoked` и `access_expired` — `user_id`, `server_id`; `config_viewed` — `server_id`. У записей,
сделанных до этого, ключей нет, и объект в журнале не называется.

`credential_imported` — `access_grant_id`, `server_id`, `user_id`, `public_key`. Имя клиента и email в аудит
не пишутся; `vpn_user_created` при импорте — `{source: import}`.
