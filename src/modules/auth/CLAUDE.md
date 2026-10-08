# modules/auth — админы панели, вход, сессии, права

`Admin` — администратор панели (не путать с `VPNUser` — клиентом VPN, они не связаны).

## Модели

- `admins`: email (unique), `password_hash` (Argon2id), `is_active`, `role` (`AdminRole`: superadmin/operator/viewer),
  `last_login_at`.
- `admin_sessions`: `admin_id`, `token_hash` (хеш refresh-токена — сам токен в БД не хранится), `expires_at`.

## Вход и токены

- `POST /api/v1/auth/login` → access JWT в ответе + refresh в HttpOnly cookie. `refresh` — ротация токена атомарно
  (`AdminSessionRepository.take`: DELETE … RETURNING) и проверка `is_active`. `logout`, `GET /me` (`AdminRead`
  с `permissions` — фронт по ним решает, какие кнопки показать).
- JWT: проверяются `type` и `sub`, иначе 401. Argon2 считается в `asyncio.to_thread` (не блокирует event loop).
- Защита от перебора (`rate_limits.py`): вход по IP (10/мин), refresh по IP (30/мин), неудачи по паре email+IP
  (5 за 15 мин) и по email со всех IP (50) — проверка **до** пароля, одинаково для несуществующих email;
  тайминг-атака закрыта `DUMMY_PASSWORD_HASH`. Блокировка → аудит `login_locked`.

## RBAC (`permissions.py`, `dependencies.py`)

Каждый маршрут требует ровно одно право: `dependencies=[Depends(require(Permission.X))]`.

| Право | viewer | operator | superadmin |
|---|---|---|---|
| servers:read, vpn_users:read, access:read, jobs:read, audit:read | ✓ | ✓ | ✓ |
| servers:check (sync/health), vpn_users:manage, access:manage, config:reveal | | ✓ | ✓ |
| servers:manage (сервер, SSH, ключ хоста, установка, протокол), admins:manage | | | ✓ |

Новый эндпоинт — обязательно с `require(...)`; `tests/integration/test_rbac.py` обходит все маршруты и проверяет
матрицу (новый маршрут без права уронит тест).

## /api/v1/admins (`admin_service.py`, только superadmin)

Список, создание, просмотр, PATCH (роль, активность, пароль). Нельзя менять себя; нельзя убрать последнего
активного superadmin (проверка под `FOR UPDATE`). Смена роли или отключение → удаление всех сессий админа.
Пароль — не короче 12 символов (`MIN_PASSWORD_LENGTH`).

## Аудит

`login`, `login_failed`, `login_locked`, `logout`, `admin_created`, `admin_updated`. Для анонимных действий
(вход) актор — `get_anonymous_actor` (только IP).

Создать админа из консоли: `PYTHONPATH=src uv run python -m scripts.create_admin email` (getpass на Windows не
читает pipe — в скриптах создавать кодом).
