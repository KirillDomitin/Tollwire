# modules/access — подписки, доступы, VPN-клиенты

`VPNUser` → `Subscription` (общий срок) → `AccessGrant` (сервер + протокол) → `VPNCredential`.
Срок принадлежит подписке, у гранта только статус для частичного отзыва сервера.

## Модели (`models/`)

- `subscriptions`: user_id (CASCADE), `status` (`AccessGrantStatus`), `valid_from`, `valid_until`, `revoked_at`.
  Одна незавершённая (PENDING/ACTIVE) подписка на пользователя — частичный уникальный индекс.
- `access_grants`: subscription_id, server_id, protocol_id (все CASCADE), `status`, `revoked_at`. Своего срока
  нет; `user_id` не дублируется (берётся через подписку). Частичный уникальный индекс (subscription_id,
  protocol_id) для незавершённых.
- `vpn_credentials`: история конфигов и резерваций, `state`, `is_gateway`, `device_name`, `peer_name`,
  `request_id`, ключ/адрес, зашифрованные config/PSK, `source`, `external_name`, `revoked_at`.
  ACTIVE требует ключ/адрес и конфиг у ISSUED; pending допускает NULL. IMPORTED не имеет приватного ключа.
  Один живой credential на grant гарантируется только для NOT is_gateway; шлюзы — до пяти на VPNUser.
- `AccessGrantStatus`: PENDING → ACTIVE → EXPIRED / REVOKED (два последних — терминальные).

## Правила (`service.py`)

- `inactive_reason(grant, subscription, now)` — единое правило «доступ живой» (статусы + даты); его используют
  выдача, показ конфига и sync.
- Подписка создаётся PENDING; планировщик (правило `access`) переводит её в ACTIVE, когда наступил `valid_from`,
  и в EXPIRED после `valid_until`. Грант становится ACTIVE, когда для него реально создан клиент (provision).
  REVOKED главнее истечения. Отзыв и истечение подписки каскадом переводят незавершённые гранты.
- Грант: на вход только `protocol_id`, сервер выводится из протокола. Конфликт — уже есть живой грант того же
  пользователя на тот же сервер и протокол. Выдача до `valid_from` запрещена (409).
- Отзыв и истечение гранта/подписки ставят адресные задачи уборки для всех credentials и резерваций;
  слот освобождается после подтверждённого удаления peer.
- Удалить грант, подписку или пользователя с живым клиентом нельзя (409) — сначала отзыв.
- Всё идемпотентно: повторный revoke не меняет `revoked_at` и не падает.
- `stage_create` у `SubscriptionService` и `AccessGrantService` — проверки, flush и аудит без commit; `create`
  = `stage_create` + commit. Регистрация бота вызывает их в одной транзакции с пользователем и задачей.

## VPN-клиент (`VPNCredentialService`)

Перед работой над выдачей, отзывом, sync или миграцией читать [контракт устройств](../../../docs/access/gateway-devices.md).

- `stage_issue`: VPNUser FOR UPDATE → проверка лимита/доступа → credential RESERVED и Job в одной транзакции.
  `schedule_issue` коммитит и enqueue. Лимит пять — все gateway-credentials пользователя без revoked_at.
- Gateway: CREATE_DEVICE/REVOKE_DEVICE; остальные протоколы: CREATE_USER/REVOKE_ACCESS. Новые задачи
  адресуют params.credential_id, legacy без target сохраняют grant-семантику. Worker проверяет grant/server.
- PROVISIONING фиксируется до SSH. Retry неоднозначной выдачи сначала подтверждает уборку точного peer;
  ACTIVE retry не меняет ключи. Подпись не идёт в SSH; dev-base32(UUID) — 30 символов, legacy-имена сохранены.
- Начало/завершение выдачи перечитывает credential под блокировкой; отзыв нельзя затереть ACTIVE.
  Failed Job не освобождает слот. Индивидуальный отзыв не меняет соседние credentials и grant.
- Для gateway после commit вызываются attach/detach. Нет исправного выхода — конфиг сохранён без интернета;
  карта и ограничения — `modules/gateway/CLAUDE.md`.
- `render_config(credential, grant)` → `RenderedConfig` (`config` — .conf, `share_link` — `vpn://`, `share_qr` — серия QR;
  имя сервера → `description`) — единственное место расшифровки; доступ и аудит проверяет вызывающий.
- `reveal_config`: config_viewed фиксируется до расшифровки; no-store, config:reveal (у viewer нет).
  Бот-ссылка проверяет доступ сама, открытие без аудита (`modules/bot/CLAUDE.md`).
- У импортированного (`encrypted_config` NULL) `reveal_config` → `ConfigUnavailableError` (409) до записи
  в аудит; `render_config` для такого клиента тоже отказывает.

## Импорт AmneziaVPN (`import_service.py`)

- Клиент переходит в БД панели без изменения сервера: peer уже на нём. Credential с `source = IMPORTED`, конфига
  у него нет (приватного ключа у панели нет).
- Задача `IMPORT_CLIENTS` под `ServerLock`; каждый клиент в savepoint; commit делает воркер вместе со статусом
  задачи.
- Исходы по клиенту и правила — `docs/access/import.md` (читать перед работой над импортом). Запуск —
  `POST /servers/{id}/import` (`modules/servers/CLAUDE.md`).

## API

Маршруты — `api/v1/router.py`: подписки/гранты/credentials и GET/POST
`/vpn-users/{id}/gateway-devices`. PATCH гранта намеренно нет; сервер выводится из protocol_id.
Метаданные читаются с access:read, выдача/отзыв — access:manage, конфиг — config:reveal.
Импорт — POST `/servers/{id}/import`; конфиг IMPORTED возвращает 409.

## Аудит

`subscription_*`, `access_granted/updated/revoked/expired/deleted`, `credential_issued/issue_failed/revoked/
revoke_failed`, `config_viewed`, `credential_imported`, `clients_import_started/completed/failed` (пишет
`JobService`, см. `modules/jobs/CLAUDE.md`). Секреты в метаданные аудита не пишутся.

Идентификаторы meta для журнала: `subscription_updated` — `user_id`;
`access_revoked` и `access_expired` — `user_id`, `server_id`; `config_viewed` — `server_id`. Старые записи могут не иметь этих ключей.

`credential_imported` — `access_grant_id`, `server_id`, `user_id`, `public_key`. Имя клиента и email в аудит
не пишутся; `vpn_user_created` при импорте — `{source: import}`.
