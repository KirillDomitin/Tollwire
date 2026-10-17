# modules/vpn_users — VPN-пользователи

`VPNUser` — человек, которому выдают VPN. С `Admin` не связан.

- Модель `vpn_users`: username, `email` (unique, NULL допустим — у клиентов, подхваченных из AmneziaVPN; `EmailStr` во входных схемах), description, `status`
  (`VPNUserStatus`: active/disabled), таймстемпы.
- Уникален email, а не username: email — будущий идентификатор для регистрации через сайт/бота. Создание через API требует email; импорт клиентов из AmneziaVPN может
  создать пользователя без email (NULL).
- API `/api/v1/vpn-users`: список, создание, просмотр, PATCH, удаление.
- Список (`list_with_access`) отдаёт `VPNUserListItem` со сводкой `access` — последняя подписка пользователя
  (через `SubscriptionRepository.latest_by_user`, DISTINCT ON). Число серверов в сводку не входит.
- Удалить пользователя с живыми VPN-клиентами нельзя (409) — сначала отзыв доступа. Подписки, гранты и
  credential'ы удаляются каскадом.
- Аудит: `vpn_user_created/updated/deleted`.
