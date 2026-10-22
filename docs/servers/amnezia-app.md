# Серверы: поиск установки AmneziaVPN и импорт её клиентов

Перенесено из `src/modules/servers/CLAUDE.md` без изменений текста; там остались инварианты.

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
