# Проверки образов протоколов

Контракт и API — [обновление протоколов](../servers/protocol-updates.md).

| Тесты | Проверки |
|---|---|
| `unit/test_protocol_update.py`, `fakes/protocol_update.py` | AWG/шлюз, pull до stop, immutable digest, noop по ID и по manifest платформы при разных containerd index ID, несколько RepoDigests, сохранность клиентов/порта/файлов, отказ чужим контейнерам, rollback и его сбой, recovery после прерывания, сохранность новых клиентов при повторной очистке verified-операции |
| `unit/test_protocol_update_discovery.py` | Read-only image inspect, Docker 29 descriptor и legacy/null, проверка digest/платформы, выбор платформы и config digest установленного manifest, ошибка недоступного старого manifest |
| `unit/test_protocol_registry_security.py`, `services/test_protocol_registry_cache.py` | Фиксированный auth scope/host, запрет redirects, размер metadata, 429, общий Redis-кеш при конкурентных запросах |
| `services/test_protocol_update_checks.py` | Статусы по config digest при сохранении сырого Docker ID в API, кеш без сети, stale/expiry, ошибки registry, APP/удалённые протоколы, RUNNING update и гонка поколений |
| `integration/test_protocol_update.py`, `integration/test_protocol_update_checks_api.py` | API/RBAC, body/query, дедуп задач, dispatch/result/audit, подробности FAILED |
| `integration/test_protocol_update_schedule.py` | 03:00 Москва, календарный дедуп, догоняющий запуск, retry после ошибки инфраструктуры, изоляция серверов |
| `services/test_protocol_update_migration.py` | Enum задач, полный цикл миграций и точечный downgrade/upgrade; удаляются только новые типы задач, старые данные сохраняются |
| `frontend/src/pages/server/ProtocolUpdate.test.tsx`, `JobsLog.protocolUpdates.test.tsx` | Статус конкретного протокола, права, подтверждение разрыва, ручная проверка, stale, результаты замены/noop/отката и ru/en |

Фейк моделирует full CID, cidfile, snapshot, атомарный журнал, ошибки запуска/очистки и обрыв отката.
Незнакомые команды остаются ошибкой. Эти тесты не заменяют проверку Docker на выделенном VPN-сервере.

## Живая проверка Docker 29.6.1

На изолированных AWG и шлюзе после исправления подтверждены замена с сохранением состояния,
статус `current` по registry и повторное обновление без смены CID. Временные объекты удалены,
исходные контейнеры сервера не изменились. До исправления на обоих протоколах также проверены
реальный rollback, recovery после rename и сохранность нового клиента при очистке `verified`;
эти ветки исправление не меняет. Noop при разных index ID и одинаковом manifest платформы покрыт
двумя unit-сценариями; живого подтверждения этого варианта нет.
