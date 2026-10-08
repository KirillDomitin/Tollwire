# modules/protocols — VPN-протокол на сервере

Сейчас один тип — `ProtocolType.AMNEZIA_WG`. Своего роутера нет: протоколом управляют эндпоинты серверов
(`POST/DELETE /api/v1/servers/{id}/protocol`, `GET /api/v1/servers/{id}/protocols`).

- Модель `protocols`: `server_id` (FK CASCADE), `type`, `port` (реальный UDP-порт из конфига сервера);
  `UniqueConstraint(server_id, type)`. Статуса нет: строка есть — протокол установлен.
- `ProtocolRepository.create` работает как get-or-update (повторная установка не дублирует строку);
  `delete` идемпотентен.
- Провайдер по типу протокола выбирает `provider_class()` в `modules/servers/service.py` (`match`; неизвестный
  тип → `UnsupportedProtocolError`). Новый протокол = новый класс `VPNProvider` в `infrastructure/vpn/` + ветка
  в фабрике + значение enum (миграция enum-типа — см. `alembic/CLAUDE.md`).
- Удаление протокола каскадом удаляет гранты и credential'ы — поэтому снять протокол с живыми клиентами нельзя
  (проверка в `ServerService.schedule`).
