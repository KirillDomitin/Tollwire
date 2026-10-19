# modules/protocols — VPN-протокол на сервере

Типы: `ProtocolType.AMNEZIA_WG` (свой протокол панели), `AMNEZIA_GATEWAY` (шлюз: свой контейнер, подсеть `10.77.0.0/22`,
см. `infrastructure/vpn/CLAUDE.md`; панель ставит и снимает его сама, как и `AMNEZIA_WG` -
`INSTALLABLE_PROTOCOLS`) и `AMNEZIA_APP_AWG` / `AMNEZIA_APP_AWG2` — установки
приложения AmneziaVPN, «чужие» (`APP_PROTOCOLS`): панель их не ставит и не удаляет, клиентов отзывает и возвращает
(`revoke_client`/`restore_peer`), остальное читает (поиск, sync, health; см. `infrastructure/vpn/CLAUDE.md`). Своего роутера нет: протоколом управляют эндпоинты серверов
(`POST/DELETE /api/v1/servers/{id}/protocol`, `GET /api/v1/servers/{id}/protocols`).

- Модель `protocols`: `server_id` (FK CASCADE), `type`, `port` (реальный UDP-порт из конфига сервера);
  `UniqueConstraint(server_id, type)`. Статуса нет: строка есть — протокол установлен. Строк для `AMNEZIA_APP_*` панель не создаёт (API ставит только `AMNEZIA_WG`).
- `ProtocolRequest` (`schemas.py`) - необязательное тело POST/DELETE `/servers/{id}/protocol`: `type` из
  `INSTALLABLE_PROTOCOLS`, по умолчанию `amnezia_wg`. Тот же тип можно передать query `?type=` (роутер сводит оба
  источника; расхождение - 422).
- `ProtocolRepository.create` работает как get-or-update (повторная установка не дублирует строку);
  `delete` идемпотентен.
- Провайдер по типу протокола выбирает `provider_class()` в `modules/servers/service.py` (`match`; неизвестный
  тип → `UnsupportedProtocolError`). Новый протокол = новый класс `VPNProvider` в `infrastructure/vpn/` + ветка
  в фабрике + значение enum (миграция enum-типа — см. `alembic/CLAUDE.md`).
- Удаление протокола каскадом удаляет гранты и credential'ы — поэтому снять протокол с живыми клиентами нельзя
  (проверка в `ServerService.schedule`).
