# modules/vpn_users — VPN-пользователи

`VPNUser` — человек, которому выдают VPN. С `Admin` не связан.

- Модель `vpn_users`: username, `email` (unique, NULL допустим — у клиентов, подхваченных из AmneziaVPN; `EmailStr` во входных схемах), description, `status`
  (`VPNUserStatus`: active/disabled), таймстемпы.
- Telegram-поля: `telegram_id` (BIGINT, UNIQUE, NULL) и `telegram_username` (до 64, для показа). Заполняет
  только регистрация через бота (`modules/bot`); у остальных NULL.
- Уникален email, а не username: email — будущий идентификатор для регистрации через сайт/бота. Создание через API требует email; импорт клиентов из AmneziaVPN может
  создать пользователя без email (NULL).
- API `/api/v1/vpn-users`: список, создание, просмотр, PATCH, удаление.
- Список (`list_with_access`) отдаёт `VPNUserListItem` со сводкой `access` — последняя подписка пользователя
  (через `SubscriptionRepository.latest_by_user`, DISTINCT ON). Число серверов в сводку не входит.
- Удалить пользователя с живыми VPN-клиентами нельзя (409) — сначала отзыв доступа. Подписки, гранты и
  credential'ы удаляются каскадом.
- Аудит: `vpn_user_created/updated/deleted`. При регистрации через бота `vpn_user_created` с `{source: bot}`.
- `stage_create(data, fields=..., audit_meta=...)` — без commit: регистрация бота пишет пользователя, подписку
  и грант одной транзакцией. `create` = `stage_create` + commit. `get_by_telegram_id` — поиск по Telegram.
  `get_by_email_ignore_case` — почта без учёта регистра, только для пути бота; обычное создание сравнивает
  почту с учётом регистра.
