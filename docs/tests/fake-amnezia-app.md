# Фейковый SSH: приложение AmneziaVPN (чтение и запись, этапы 23.x)

Перенесено из `tests/CLAUDE.md` без изменений текста; там остались окружение, правила и основа фейка SSH.

- Приложение AmneziaVPN (контейнеры `amnezia-awg` с `wg`/`wg0` и `amnezia-awg2` с `awg`/`awg0`, конфиг и `clientsTable`
  внутри контейнера, монтирование не из папки панели): `add_amnezia_app(container, peers, clients_table, running, port)`
  возвращает `AmneziaApp` (`secrets` — серверный приватный ключ и PSK для проверок «не утекло», `files`, `interface_up`).
  Чтение внутри контейнера — только `docker exec <к> cat /opt/amnezia/awg/<файл>` и `<wg|awg> show <интерфейс>
  latest-handshakes`; чужая утилита → код 126, остановленный контейнер → ошибка Docker, прочее → `AssertionError`.
  `changing_commands()` — как `mutating_commands()`, но эти два чтения изменением не считаются (для провайдеров
  чужого контейнера; `mutating_commands()` остаётся строгим — любой docker exec там изменение). `container_reads` —
  журнал прочитанных внутри контейнеров файлов (проверка «секретные файлы не читали»).
- Запись в приложение (этап 23.3, `test_amnezia_app_write.py`, `integration/test_app_revoke.py`): фейк понимает
  `docker cp <STAGE_DIR>/<контейнер>.conf|.clientsTable <контейнер>:<конфиг|clientsTable>` (иные пути и чужие
  контейнеры, в том числе контейнер панели, — `AssertionError`), `rm -f <файл из STAGE_DIR>` и
  `docker exec <к> bash -c '<утилита> syncconf <iface> <(<утилита>-quick strip <конфиг>)'` (разбирает конфиг и
  обновляет «живых» `app.peers`; мусорный конфиг — код 1). `stage_files()` — промежуточные файлы на хосте (после
  любой операции их быть не должно), `unexpected_changing_commands()` — изменяющие команды сверх протокола записи,
  `copies` (что и с какими правами скопировали), `file_modes` (SFTP всегда 0600), `write_attempts`. Сбои и гонки:
  `app.apply_fails`, `app.docker_cp_fails` (пути в контейнере), `fake.write_fails` (пути SFTP на хосте),
  `app.on_read(путь, номер_чтения)` — подмена файла «приложением» между чтением и записью, `app.on_apply` — действие
  перед syncconf (например, сломать откат). `mutating_commands()` остаётся строгим. Дополнительно: `mkdir -p
  /opt/vpn-admin` и `ensure_private_dir` (фейк запоминает 0700 в `dirs`) — как настоящие; `rm -f` вне `STAGE_DIR` —
  `AssertionError`; `app.read_counts` (сколько раз прочитан путь), `app.applies` (сколько раз syncconf применил
  конфиг). Сбой `strip` фейк не моделирует (`<утилита>-quick strip` в фейке всегда успешен), как и пустой конфиг,
  сносящий peer'ов: это проверено только на живом контейнере (см. «Грабли» в `src/infrastructure/vpn/CLAUDE.md`).
