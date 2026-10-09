# VPN Control Panel

REST API + веб-интерфейс для управления VPN-серверами AmneziaWG: серверы, VPN-пользователи, доступы, выдача
конфигов, установка по SSH, фоновые задачи, мониторинг, аудит. Детали — в `CLAUDE.md` каждой папки; запуск и
деплой — `INSTALL.md`.

## Как работать

- Архитектурную развилку — на решение пользователя до кода, с рекомендацией.
- После изменений: ruff, mypy, тесты, миграции (upgrade/downgrade) и живой прогон.
- Изменения на реальных серверах — только с явного согласия пользователя.
- Поменялось устройство модуля — обновить его `CLAUDE.md`.
- Коммиты без Co-Authored-By. Документация библиотек — через Context7.

## Архитектура

- Слои сверху вниз: router → service → repository / `VPNProvider` → SSH/БД. В роутерах только HTTP, схемы,
  `require(Permission.X)` и вызов сервиса.
- VPN — только через `VPNProvider`; бизнес-логика не знает SSH, Docker и форматов конфигов.
- `infrastructure/` не импортирует `modules/` (кроме воркера и планировщика).
- Доменные ошибки — `core/exceptions.py`, в HTTP-коды их переводит `main.py`.

## Безопасность

Секреты (SSH, VPN-конфиги) — в БД только зашифрованными; `ENCRYPTION_KEY` и `.env*` — не читать, не коммитить.
Никакого пользовательского ввода в SSH-команды. В логи — никаких паролей, ключей, токенов, конфигов.

## Где что

`src/core` общее · `src/infrastructure/{ssh,vpn,workers,monitoring}` внешний мир · `src/modules/*` домен
(auth, servers, protocols, vpn_users, access, jobs, audit, monitoring) · `src/scripts` утилиты · `alembic` миграции
· `tests` · `frontend` интерфейс.

## Команды

```bash
uv run uvicorn main:app --app-dir src --reload   # API
uv run python src/worker.py                      # воркер + планировщик
uv run ruff check . && uv run mypy src && uv run pytest
```
