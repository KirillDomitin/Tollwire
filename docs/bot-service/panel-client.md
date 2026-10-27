# bot — клиент панели (`panel.py`)

Инварианты — в `bot/CLAUDE.md`. Здесь — адрес, TLS, повторы, соответствие кодов ответов исключениям.

## Адрес и запрос

- База: `PANEL_URL` + `/api/v1/bot`. Заголовки: `X-Bot-Key` и `User-Agent: tollwire-bot/<версия>`.
- `https://` — любой адрес. `http://` — только если имя хоста без точки и без двоеточия (`api` в сети compose).
  Иначе `ValueError` при создании клиента: бот не стартует.
- Таймауты: 10 с, соединение — 5 с. `follow_redirects=False`: ключ не уходит на другой адрес, а переадресация —
  `PanelUnexpected`.

## TLS (`_ssl_context`)

- Контекст строится на `certifi.where()`. Если задан `PANEL_CA_FILE`, его сертификаты добавляются поверх штатных
  (Let's Encrypt продолжает работать).
- Только при `PANEL_CA_FILE` снимается `VERIFY_X509_STRICT`: самоподписанный сертификат панели закреплён отпечатком в
  установщике, строгие проверки профиля X.509 (нет SKI/AKI и т.п.) отрезали бы бота от своей панели.

## Методы

| Метод клиента | Запрос | Результат |
|---|---|---|
| `heartbeat(bot_username, version)` | `POST /heartbeat` | ответ отбрасывается |
| `check_invite(code)` | `POST /invites/check` | строка `status` |
| `register(code, telegram_id, telegram_username, name, email)` | `POST /registrations` | `Registration(registration_id, status, config_url)` |
| `registration(registration_id)` | `GET /registrations/{id}` | `Registration` или `None` (404) |
| `user_by_telegram(telegram_id)` | `GET /users/by-telegram/{id}` | `UserStatus(name, valid_until, protocol, config_url)` или `None` (404) |

Ответ, который не объект, не JSON, или без обязательного поля (`status`, `registration_id`, `name`), — `PanelUnexpected`.

## Повторы

- Сбой сети (`httpx.TransportError`), 502, 503, 504 — пауза 0.5, 1, 2, 4 с и повтор. После последней паузы —
  `PanelUnavailable`. Повтор безопасен для всех методов: `POST /registrations` идемпотентен на стороне панели.
- 429: `Retry-After` в секундах. Если заголовка нет, он больше 10 с или это уже второй 429 в запросе — `RateLimited`
  (бот говорит «подождите минуту»). Иначе ждём и повторяем один раз.
- Паузы — через `sleep`, подменяемый в тестах.

## Коды ответов

| Код | Исключение или результат | Примечание |
|---|---|---|
| 2xx | ответ | — |
| 401 | `PanelAuthError` | без повтора; ERROR в лог один раз, пока панель не ответит иначе |
| 404 | `None` (только у `registration` и `user_by_telegram`), иначе `PanelUnexpected` | |
| 409 | `Conflict(reason)` | `reason` из списка `CONFLICT_REASONS`, незнакомый → `conflict` |
| 422 | `Rejected(field)` | `field` — последний элемент `loc` из `detail[0]`; `detail` целиком не читается и не логируется |
| 429 | `RateLimited` или повтор | см. выше |
| 502, 503, 504 | повторы, затем `PanelUnavailable` | |
| прочие (3xx, 400, 500) | `PanelUnexpected("HTTP <код>")` | |

Сеть, таймаут и сертификат, который не прошёл проверку, — `PanelUnavailable` (сертификат — тоже транспортная ошибка
`httpx`).

## Исключения

| Класс | Когда |
|---|---|
| `PanelError` | база |
| `PanelAuthError` | ключ не принят (401) |
| `PanelUnavailable` | панель не отвечает |
| `PanelUnexpected` | ответ не по контракту |
| `RateLimited` | слишком частые запросы |
| `Conflict(reason)` | 409 |
| `Rejected(field)` | 422 |

Все исключения несут код или причину, но не тело запроса и ответа: там бывают имя, почта, код приглашения.
Диалог переводит их в тексты через `problem_text` (`dialog.py`): `RateLimited` — `rate_limited`, остальное —
`panel_unavailable`. Про ключ и сертификат человеку не говорится.
