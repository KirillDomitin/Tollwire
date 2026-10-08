# modules/access — подписки, доступы, VPN-клиенты

Цепочка: `VPNUser` → `Subscription` (срок) → `AccessGrant` (сервер + протокол) → `VPNCredential` (клиент на
сервере и его зашифрованный конфиг).

⚠️ Отступление от ТЗ §18 (плоская таблица AccessGrant со своим сроком у каждой строки): срок вынесен в
`Subscription` — одна подписка на несколько серверов истекает разом; у гранта свой статус для частичного отзыва.
Это точка расширения под будущие тарифы и оплату.

## Модели (`models/`)

- `subscriptions`: user_id (CASCADE), `status` (`AccessGrantStatus`), `valid_from`, `valid_until`, `revoked_at`.
  Одна незавершённая (PENDING/ACTIVE) подписка на пользователя — частичный уникальный индекс.
- `access_grants`: subscription_id, server_id, protocol_id (все CASCADE), `status`, `revoked_at`. Своего срока
  нет; `user_id` не дублируется (берётся через подписку). Частичный уникальный индекс (subscription_id,
  protocol_id) для незавершённых.
- `vpn_credentials`: access_grant_id (CASCADE), `encrypted_config` (Fernet), `client_address`,
  `client_public_key`, `revoked_at`. История перевыпусков + не больше одного живого на грант (частичный индекс
  `WHERE revoked_at IS NULL`).
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

## API

`/vpn-users/{id}/subscriptions` (GET/POST) · `/subscriptions/{id}` (GET/PATCH/DELETE) · `/subscriptions/{id}/revoke`
· `/subscriptions/{id}/grants` (GET/POST) · `/access/{id}` (DELETE) · `/access/{id}/revoke` ·
`/access/{id}/credentials` (GET, POST — выдать клиента) · `/credentials/{id}` · `/credentials/{id}/config` (POST)
· `/credentials/{id}/revoke`. PATCH гранта удалён намеренно.

## Аудит

`subscription_*`, `access_granted/updated/revoked/expired/deleted`, `credential_issued/issue_failed/revoked/
revoke_failed`, `config_viewed`. Секреты в метаданные аудита не пишутся.
