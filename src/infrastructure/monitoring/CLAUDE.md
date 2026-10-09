# infrastructure/monitoring — замер Linux-сервера

`host_metrics.py`: одна команда по SSH → разбор → скорости. Ничего не знает о БД и модулях; хранение и API —
`src/modules/monitoring/`.

- `HOST_METRICS_COMMAND` — константа только для чтения, разделы помечены строками `@имя`: `@uptime`, `@stat`,
  `@loadavg`, `@meminfo`, `@df`, `@diskstats`, `@netdev`, `@route`. Пользовательского ввода в команде нет и быть
  не должно.
- `parse_host_metrics(output) -> HostSample` — счётчики (`HostCounters`) + мгновенные значения. Основной сетевой
  интерфейс — из маршрута по умолчанию, запасной — самый нагруженный не служебный (`lo`, `docker*`, `br-*`,
  `veth*`, `awg*`, `wg*`, `tun*` исключены). Диски — только целые устройства (`sd*`, `vd*`, `nvme*n*` …), сектор
  = 512 байт.
- `compute_rates(previous, current) -> HostRates | None` — скорость = разница счётчиков / разница аптайма
  **сервера** (не часов панели). Перезагрузка или сброс счётчика → `None`.
- `read_host_sample(ssh_client)` — выполнить команду и разобрать.
- Новая метрика: дописать раздел в команду, разбор, поле в `HostSample`/`HostRates`, научить фейк
  (`tests/fakes/fake_ssh.py`, `host_stats`) и добавить случай в `tests/unit/test_host_metrics.py`.
