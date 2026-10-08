# infrastructure/ssh — подключение к серверам

`SSHClient` (`client.py`) — обёртка над AsyncSSH; `host_keys.py` — ключи хостов (`HostKey`, `parse_host_key`,
`scan_host_key` — узнать ключ без входа).

## SSHClient

- Async context manager: `async with client:` → `connect()` … `close()`. Без `async with` — RuntimeError.
- `execute(command, timeout=None) -> CommandResult(stdout, stderr, exit_status)`; `timeout` переопределяет
  `SSH_COMMAND_TIMEOUT` для долгих команд (docker pull — 300 с).
- `write_file(path, content)` — через SFTP: временный файл с правами 0600 + атомарный `posix_rename`;
  папки — `sftp.makedirs(posixpath.dirname(path))` (пути всегда POSIX, не `os.path`).
- `read_file(path)` — через SFTP.
- Ретраи с backoff только на `connect()` и только для временных ошибок; ошибки AsyncSSH/OSError → `SSHError`,
  `SSHConnectionError`, `SSHAuthError`, `SSHTimeoutError` (`core/exceptions.py`).

## Ключ хоста (TOFU — «доверие при первом подключении» + ручное закрепление)

- В клиент передаётся закреплённый ключ сервера (`host_key`) и колбэки: «узнали ключ» / «ключ не совпал».
- Пароль отправляется **только** серверу с закреплённым ключом, иначе `SSHHostKeyNotPinnedError` (409): сначала
  scan + pin в API серверов. Несовпадение ключа → отказ до отправки пароля, без повторов.
- AsyncSSH изолирован от машины, где крутится бэкенд: `config=None`, без `~/.ssh`, без агента
  (`agent_identities=[наш ключ]`; `agent_path=None` на Windows агент НЕ выключает).
- Вход по ключу: `load_private_key(text)` — передаётся текст ключа, а не путь.

## Грабли

- Команды — только константы или значения, проверенные по белому списку. Содержимое файлов — через SFTP, не
  через `echo`/heredoc в команде (иначе инъекция и утечка в лог).
- `docker ps --filter` возвращает exit 0, даже если ничего не нашёл — проверять stdout.
- В пайпе `a | b` код возврата — от последней команды; сбой `a` не виден.
- Вывод команд — с переводом строки в конце: сравнивать `stdout.strip()`.
- f-строки и `.format` превращают `{{ }}` в `{ }` — Go-шаблоны docker (`{{.State}}`) собирать конкатенацией.
- Логируется только сама команда (DEBUG); stdout/stderr/содержимое файлов — никогда.

Тесты: `tests/unit/test_ssh_real_server.py` — настоящий AsyncSSH-сервер в процессе; остальное — через фейк
(`tests/fakes/fake_ssh.py`).
