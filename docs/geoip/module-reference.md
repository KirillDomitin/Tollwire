# Справочник (вынесено из CLAUDE.md)

Описательные части: карта файлов, модели, API, операции. Инварианты и грабли остались в CLAUDE.md.

## Таблицы

- `geoip_lists`: `source` (ripe / upload), `version`, `sha256`, `prefixes_count`, `addresses_count`, `data` (Text, весь
  список; в API не отдаётся), `status` (`GeoipStatus`, VARCHAR: active / candidate / superseded), `created_at`,
  `activated_at`. Прежние списки не удаляются (история, откат вручную через новую загрузку).
- `geoip_overrides`: `network` (VARCHAR(18), UNIQUE), `action` (`direct` - всегда напрямую; `tunnel` - всегда через выход),
  `note` (до 100), `created_at`. Сеть проверяет `validate_override`: строгий IPv4 CIDR без хост-битов. DIRECT - не крупнее /16 и
  без пересечения с зарезервированными диапазонами (иначе 422), и не накрывает адрес шлюза или выхода (422
  "must not cover the address of a gateway or an exit server"). TUNNEL только вычитает: любая сеть не крупнее /8, в том числе
  зарезервированная. Повтор сети - 409.

## API (`api/v1/router.py`, `/api/v1/geoip`)

| Метод | Право | Что |
|---|---|---|
| `GET /geoip` | `servers:read` | `{active, candidates}`: версия, число префиксов и адресов, sha256, статус; самих префиксов нет |
| `POST /geoip/update` | `servers:manage` | 202 `JobRead` (`geoip_update`, глобальная, без `server_id`); незавершённая такая же - 409 |
| `POST /geoip/upload` | `servers:manage` | тело `text/plain` (RIPE или CIDR) до 5 МБ -> `{status: active / candidate / unchanged, list_id, version, prefixes, sha256, skipped, rejected}`; больше лимита - 413 (по `Content-Length` и при потоковом чтении), плохой список - 422 |
| `POST /geoip/{id}/activate` | `servers:manage` | кандидат -> активный; 404 нет списка, 409 не кандидат |
| `GET /geoip/overrides` | `servers:read` | исключения |
| `POST /geoip/overrides` | `servers:manage` | `{network, action, note?}` -> 201; 422 неверная / непубличная / крупнее /8; 409 сеть уже есть |
| `DELETE /geoip/overrides/{id}` | `servers:manage` | 204; 404 |

Применение на конкретный шлюз вручную - `POST /api/v1/servers/{id}/gateway/ru/apply` (`gateway/api/v1/router.py`).

## Аудит

`geoip_list_updated` (версия, число префиксов, sha256, статус, источник), `geoip_list_activated`, `geoip_override_added` /
`geoip_override_removed` (сеть и действие), сущность `geoip`; `gateway_ru_applied` / `gateway_ru_apply_failed` (сущность
`server`, meta: `job_id`, число маршрутов `routes`, у сбоя `error`). Самих префиксов в журнале нет.
