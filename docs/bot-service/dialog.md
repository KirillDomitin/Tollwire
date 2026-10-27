# bot — диалог, опрос готовности, команды

Инварианты и устройство — в `bot/CLAUDE.md`. Здесь — шаги, отказы и расписание. Тексты лежат в
`bot/src/tollwire_bot/texts.py`; в таблицах названы их ключи.

## /start и регистрация (`dialog.py`)

| Шаг | Что происходит | Что видит человек |
|---|---|---|
| `/start` без кода | состояние сбрасывается | `no_code` |
| код не из алфавита `start` | — | `bad_link` |
| `GET users/by-telegram` → 200 | — | `already_registered` |
| `invites/check` → `valid` | состояние `name`, в данных код | `ask_name` |
| `invites/check` → `used`, `expired`, `revoked`, `unknown` | — | `invite_used`, `invite_expired`, `invite_revoked`, `invite_unknown` |
| имя | `clean_name`: 1–64 после strip, без служебных символов; состояние `email` | `ask_email` или `bad_name` |
| почта | `clean_email`: regex, до 100 символов; состояние `confirm` | `confirm` с кнопками «Да» / «Изменить», или `bad_email` |
| «Изменить» | состояние `name`, кнопки снимаются | `ask_name` |
| «Да» | состояние `submitting`, кнопки снимаются, `POST /registrations` | `preparing` при 201 (дальше — опрос) |

`/status`, `/config`, `/help` работают и посреди диалога, диалог не сбивают (команды регистрируются раньше состояний).
Прочий `/…` в шагах имени и почты → `unknown_input`. `/start` посреди диалога начинает его заново.

Двойное «Да»: события одного человека идут по очереди (`SimpleEventIsolation`), повторное нажатие в `submitting`
ничего не делает. Если бота перезапустили посреди отправки, `submitting` остаётся в хранилище: следующее сообщение
возвращает `confirm` (данные на месте) или сбрасывает диалог.

### Ответы панели на `POST /registrations`

| Ответ | Что делает бот | Что видит человек |
|---|---|---|
| 201 | регистрация уходит в `Watcher`, состояние сбрасывается | `preparing` |
| 409 `email_taken` | состояние `email`, имя и код остаются | `email_taken` |
| 409 `telegram_taken` | диалог закончен | `already_registered` |
| 409 `invite_used` / `invite_expired` / `invite_invalid` | диалог закончен | `invite_used` / `invite_expired` / `invite_unknown` |
| 409 с другой причиной (в т.ч. `conflict`) | диалог закончен | `conflict_generic` |
| 422 `name` / `email` | состояние для поля | `bad_name` / `bad_email` |
| 422 другое поле | диалог закончен | `conflict_generic` |
| `PanelUnavailable` (сеть, 502/503/504) | остаётся в `confirm`, «Да» можно нажать ещё раз | `panel_unavailable` |
| `RateLimited` (429 дольше 10 с) | то же | `rate_limited` |
| прочее исключение | то же, в лог — traceback | `panel_unavailable` |

Причины 409 переводятся по `CONFLICT_ENDS`, статусы приглашения из `invites/check` — по `INVITE_PROBLEMS` (`dialog.py`).
Незнакомый статус приглашения читается как `invite_unknown`, незнакомая причина 409 — как `conflict_generic`.

## Опрос готовности (`watcher.py`)

- Отсчёт идёт от момента регистрации: 2, 5 и 10 с, затем каждые 10 с до 180 с (`SCHEDULE`).
- Ответ `GET /registrations/{id}`:

| Что пришло | Итог |
|---|---|
| 404 | запись снимается, слежение закончено |
| `failed` | `failed`: «Не получилось подготовить доступ. Мы уже знаем об этом и разберёмся.» |
| `active` | `POST …/config-link` → ссылка: `ready_link` (срок и адрес страницы, предупреждение не пересылать); слежение закончено |
| `active`, `config-link` → 409 `not_ready` | ждём следующий срок |
| `active`, `config-link` → 409 `config_unavailable`, `no_access`, `not_configured` | свой текст: `config_unavailable`, `config_no_access`, `config_not_configured`; слежение закончено |
| `active`, `config-link` → 404, 409 с другой причиной, сбой панели | ждём следующий срок |
| `payment_pending`, `provisioning` | ждём следующий срок |
| сбой панели между опросами | ждём следующий срок |

- Итог «сказать» не вышел (сеть, Telegram 5xx) → попытка в следующий срок; на ней бот выпускает новую ссылку, прежняя гаснет. Telegram 403 (бот заблокирован) или 400
  (чат пропал) → пишем в лог, слежение снимается.
- После 180 с без итога: последний опрос (если ещё не было), затем `still_preparing` и снятие слежения.
- Слежения лежат в таблице `watches` хранилища. После перезапуска бота `on_startup` запускает их заново с исходным
  временем; старше 24 ч строка уходит при очистке.

## Команды (`commands.py`)

- Меню команд: `status`, `config`, `help` (ключи `cmd_status`, `cmd_config`, `cmd_help`).
- `/status`: `GET users/by-telegram`. Нет пользователя → `not_registered`; сбой панели → `panel_unavailable` или
  `rate_limited`. Ответ — `status` (имя, «Доступ до: ДД.ММ.ГГГГ», протокол или «-», строка «Ссылка на конфиг: /config»).
- `/config`: `POST users/by-telegram/{id}/config-link`. 200 → `config_link` (адрес страницы и срок); прежняя ссылка
  гаснет. Нет пользователя → `not_registered`. 409 по `reason`: `config_unavailable`, `config_no_access`,
  `config_not_ready`, `config_not_configured`; прочая причина → `conflict_generic`. Сбой панели — как у `/status`.
  В группах молчит.
- `/help` → `help` (перечисляет `/config`).
- Callback, который не поймал диалог, — только `answer()`; текст, который не поймал ни диалог, ни команда → `unknown_input`.

## Ограничение частоты (`app.py`)

`UserThrottle` на каждый апдейт человека: не больше 60 событий за 60 с. Лишние отбрасываются молча; сообщение
`rate_limited` уходит раз в окно. Callback при лимите подтверждается `answer()`. Очередь времён на человека чистится,
когда словарь переваливает за 10 000 ключей.

## Heartbeat (`heartbeat.py`)

- `getMe` один раз при старте: неверный токен (`TelegramUnauthorizedError`) — выход с кодом 1 и сообщением
  «check TELEGRAM_BOT_TOKEN».
- Затем `POST /heartbeat` с именем бота и версией каждые 30 с. Сбой любого типа не останавливает бота.
- В лог — только смена состояния: «панель не отвечает» (warning) и «панель снова отвечает» (info).
