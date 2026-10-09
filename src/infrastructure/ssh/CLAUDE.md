# infrastructure/ssh — подключение к серверам

`SSHClient` (`client.py`) — обёртка над AsyncSSH; `host_keys.py` — ключи хостов (`HostKey`, `parse_host_key`,
`scan_host_key` — узнать ключ без входа); `authorized_keys.py` — правка ~/.ssh/authorized_keys и генерация ключей.

## SSHClient

- Async context manager: `async with client:` → `connect()` … `close()`. Без `async with` — RuntimeError.
- `execute(command, timeout=None) -> CommandResult(stdout, stderr, exit_status)`; `timeout` переопределяет
  `SSH_COMMAND_TIMEOUT` для долгих команд (docker pull — 300 с).
- `write_file(path, content)` — через SFTP: временный файл с правами 0600 + атомарный `posix_rename`;
  папки — `sftp.makedirs(posixpath.dirname(path))` (пути всегда POSIX, не `os.path`).
- `read_file(path)` — через SFTP.
- `read_file_optional(path) -> str | None` — файл или None, если его нет; симлинк/ошибки — исключение.
- `home_dir() -> str` — домашний каталог пользователя по SFTP realpath(".").
- `ensure_private_dir(path)` — каталог 0700: создать или ужесточить права; симлинк → отказ.
- Ретраи с backoff только на `connect()` и только для временных ошибок; ошибки AsyncSSH/OSError → `SSHError`,
  `SSHConnectionError`, `SSHAuthError`, `SSHTimeoutError` (`core/exceptions.py`).

## Ключ хоста (TOFU — «доверие при первом подключении» + ручное закрепление)

- В клиент передаётся закреплённый ключ сервера (`host_key`) и колбэки: «узнали ключ» / «ключ не совпал».
- Пароль отправляется **только** серверу с закреплённым ключом, иначе `SSHHostKeyNotPinnedError` (409): сначала
  scan + pin в API серверов. Несовпадение ключа → отказ до отправки пароля, без повторов.
- AsyncSSH изолирован от машины, где крутится бэкенд: `config=None`, без `~/.ssh`, без агента
  (`agent_identities=[наш ключ]`; `agent_path=None` на Windows агент НЕ выключает).
- Вход по ключу: `load_private_key(text)` — передаётся текст ключа, а не путь; `private_key_fingerprint(text)` —
  отпечаток (SHA256:…) без соединения.

## `authorized_keys.py` — правка и установка ключей

- Чистые функции: `render_line()` (строка в authorized_keys), `add()`, `remove_blob()`, `remove_marker()`
  (правка текста файла; сохраняют CRLF, добавляют \n если его нет, не трогают чужие строки).
- `generate_client_key(comment)` — новый ed25519-ключ (никогда не спрашивает passphrase).
- Класс `AuthorizedKeys(ssh_client)`: `install(line, marker)`, `remove(blob)`, `prune(marker, keep_blob)` —
  работают через SFTP, симлинки отвергают. `marker` = `vpn-panel:<server_id>` (комментарий ключа).

## Грабли

- Команды — только константы или значения, проверенные по белому списку. Содержимое файлов — через SFTP, не
  через `echo`/heredoc в команде (иначе инъекция и утечка в лог).
- `docker ps --filter` возвращает exit 0, даже если ничего не нашёл — проверять stdout.
- В пайпе `a | b` код возврата — от последней команды; сбой `a` не виден.
- Вывод команд — с переводом строки в конце: сравнивать `stdout.strip()`.
- f-строки и `.format` превращают `{{ }}` в `{ }` — Go-шаблоны docker (`{{.State}}`) собирать конкатенацией.
- Логируется только сама команда (DEBUG); stdout/stderr/содержимое файлов — никогда.

Тесты: `tests/unit/test_ssh_real_server.py` — настоящий AsyncSSH-сервер в процессе; `test_authorized_keys*.py` —
текст файлов и сервер; остальное — через фейк (`tests/fakes/fake_ssh.py` с `home_dir`/`ensure_private_dir`/
`read_file_optional`).
