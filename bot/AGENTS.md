# Telegram-бот

Перед работой читать [справочник](../docs/bot-service/reference.md),
[клиент панели](../docs/bot-service/panel-client.md),
[PanelWatch и pacing](../docs/bot-service/panel-watch.md), [тесты](../docs/bot-service/tests.md).
Остальные инварианты — [CLAUDE.md](CLAUDE.md).

- Не импортировать `src/`; панель доступна только через Bot API.
- Секреты, коды, сообщения и адреса конфигов не писать в лог; SQLite — 0600.
- `/admin` и 32-символьный `/start` — привязка; приглашения имеют 24 символа.
- Outbox и PanelWatch используют один MessagePacer; RetryAfter приостанавливает обоих.
- Состояние инцидента сохранять по получателям; 401/429 не объявляют восстановление.
- Notify plain отправлять без parse mode, HTML — только по явному формату панели.
- Лимит памяти бота в обоих Compose — 256 МБ; 128 МБ приводит к OOMKilled/exit 137 при запуске.
