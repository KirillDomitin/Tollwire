# Дорожная карта — VPN Control Panel

Полная история этапов. Статусы: `[ ]` не начато · `[~]` в работе · `[x]` принято.
Краткая сводка и открытые этапы — в корневом `CLAUDE.md`, раздел 6. Отмечать прогресс — здесь **и** там.

### Блок 0. Фундамент
- [x] 0.1 Окружение: зависимости (uv), `pyproject`, структура, Ruff+mypy, `.env`/`.env.example`
- [x] 0.2 Docker Compose для dev: postgres + redis
- [x] 0.3 Конфиг через pydantic-settings (`core/config.py`)
- [x] 0.4 Async-подключение к PostgreSQL (`AsyncEngine`, `async_sessionmaker`, `get_session`) + healthcheck endpoint

### Блок 1. База данных и миграции
- [x] 1.1 Declarative Base (SQLAlchemy 2.x), общий базовый класс модели
- [x] 1.2 Настройка Alembic под async
- [x] 1.3 Модель `Admin` + первая миграция

### Блок 2. Аутентификация
- [x] 2.1 Хеширование паролей Argon2id (`core/security.py`)
- [x] 2.2 JWT access token (выпуск/проверка)
- [x] 2.3 Refresh token + `admin_sessions` (хранение hash) + HttpOnly cookie
- [x] 2.4 Endpoints: `login`, `refresh`, `logout`, `me`
- [x] 2.5 Dependency «текущий админ» (авторизация роутеров)

### Блок 3. Эталон слоёв
- [ ] 3.1 Паттерн repository → service → schema → router на одном простом примере

### Блок 4. Серверы
- [x] 4.1 Модель `VPNServer` + статусы (NEW/INSTALLING/READY/OFFLINE/ERROR)
- [x] 4.2 CRUD `/api/v1/servers`
- [x] 4.3 `server_credentials` + шифрование секретов (`ENCRYPTION_KEY`)

### Блок 5. SSH-подсистема
- [x] 5.1 `SSHClient` (AsyncSSH): connect/execute/upload/download/close
- [x] 5.2 Таймауты, retry, обработка ошибок аутентификации/соединения, безопасное закрытие
- [x] 5.3 `test-connection` endpoint

### Блок 6. VPN Provider
- [x] 6.1 Интерфейс `VPNProvider` (install/uninstall/get_status/create_client/revoke_client/list_clients/sync)
- [x] 6.2 Скелет `AmneziaProvider` (`AmneziaInstaller`, `AmneziaWGProvider`)

### Блок 7. Jobs и Worker
- [x] 7.1 Модель `Job` + статусы/типы
- [x] 7.2 Очередь задач на Redis
- [x] 7.3 Отдельный процесс `vpn-admin-worker` (asyncio)
- [x] 7.4 Distributed locks на Redis (TTL + безопасное освобождение)
- [x] 7.5 Endpoints `/api/v1/jobs`, `/api/v1/jobs/{id}`

### Блок 8. Установка Amnezia (сквозной сценарий)
- [x] 8.1 `install`/`uninstall` через Job → worker → SSH → verification → sync

### Блок 9. Протоколы
- [x] 9.1 Модель `VPNProtocol` (AMNEZIA_WG), установка/удаление протокола

### Блок 10. VPN-пользователи
- [x] 10.1 Модель `VPNUser` (не связан с `Admin`) + CRUD `/api/v1/vpn-users`

### Блок 11. Доступы (AccessGrant)
- [x] 11.1 Модель `AccessGrant` + статусы (PENDING/ACTIVE/EXPIRED/REVOKED)
- [x] 11.2 Выдача/изменение/отзыв доступа + проверки конфликтов
- [x] 11.3 Ограничение по времени (`valid_from`/`valid_until`) + идемпотентный revoke

### Блок 12. VPN-конфигурации
- [x] 12.1 Модель `VPNCredential` (шифрованный config) + защищённая выдача конфигурации
- [x] 12.2 Реальный `create_client`/`revoke_client` в `AmneziaWGProvider` (аллокатор адресов, `[Peer]` на сервере, сборка конфига клиента)
- [x] 12.3 Выдача/отзыв клиента через Job (`CREATE_USER`/`REVOKE_ACCESS`), эндпоинты, отзыв на сервере при отзыве гранта/подписки

### Блок 13. Синхронизация и Health
- [x] 13.1 `sync` сервера (сверка реального состояния с БД)
- [x] 13.2 `health` (SSH/Docker/Amnezia/protocol/service) + сохранение результата

### Блок 14. Аудит
- [x] 14.1 Модель `AuditLog` + запись критических действий (без секретов)

### Блок 15. Фоновые правила
- [x] 15.1 Периодический worker: перевод просроченных `AccessGrant` в EXPIRED + revoke на сервере

### Блок 16. Тесты
- [x] 16.1 Unit: сервисы, репозитории, провайдеры, переходы статусов, expiration
- [x] 16.2 Integration: PostgreSQL, Redis, API, auth (fake/mock SSH-слой)

### Блок 17. Production
- [x] 17.1 Docker: единый image для api+worker; compose: api/worker/postgres/redis/nginx
- [x] 17.2 Структурированные логи (request_id, job_id, server_id)

### Блок 18. Доводка требований ТЗ (после MVP)
- [x] 18.1 Rate limiting (Redis + грубый лимит nginx) + защита login от тайминг-атаки
- [x] 18.2 RBAC (роли superadmin/operator/viewer, права на каждый эндпоинт, /api/v1/admins)
- [x] 18.3 Проверка ключа SSH-сервера (known_hosts / TOFU)

### Блок 19. Исправления по ревью (fastapi-reviewer + security-reviewer)
- [x] 19.1 Корректность и безопасность: пароль SSH только на закреплённый ключ, смена host удаляет учётку, вход по SSH-ключу, только свои ключи (без ~/.ssh и агента), удалён PATCH /access, гонка выдачи/отзыва, TTL блокировки сервера, Argon2 вне event loop
- [x] 19.2 Надёжность: ошибки 500, зависшие задачи, rollback в воркере, сбои Redis, атомарный refresh, дубли подписок, Job.error, валидация, логика из роутеров в сервисы
- [x] 19.3 Hardening: проверка JWT_SECRET_KEY, блокировка входа по (email,IP) + общий порог, случайная обфускация AmneziaWG, allowlist клиентского конфига, атомарная запись awg0.conf, образы по дайджесту, cap_drop/read_only/no-new-privileges, openapi off в проде, dev-порты на localhost

### Блок 20. Фронтенд (React + Vite + TypeScript, `frontend/`, статика за nginx; дизайн «Журнал связи»)
- [x] 20.1 Фундамент: каркас, дизайн-токены (светлая/тёмная), вход с тихим refresh, навигация по правам, список серверов, сборка в Docker + nginx
- [x] 20.2 Сервер: карточка с чек-листом подготовки, SSH-учётка, ключ хоста (scan → pin), установка/протокол, журнал задач с опросом, опасные действия с последствиями, добавление сервера
- [x] 20.3 VPN-пользователи: список со сводкой доступа (API), карточка, «Выдать доступ» одним диалогом, срок, отзыв, показ конфига (QR + копирование + .conf)
- [ ] 20.4 Журнал аудита, задачи, админы (superadmin)

### Блок 21. Конфиг для приложения и мониторинг серверов
- [x] 21.1 Конфиг в формате AmneziaVPN (`vpn://` + серия QR как у экспорта AmneziaVPN) с именем сервера из панели; `.conf` — запасной
- [x] 21.2 Сбор метрик без агента (SSH раз в минуту): клиенты всего/онлайн, CPU, RAM, диск, сеть; хранение 7 дней + почасовые 90 дней
- [x] 21.3 Мониторинг в интерфейсе: графики на карточке сервера, сводка в списке
- [ ] 21.4 (опц.) Онлайн и трафик по клиентам

### Definition of Done (MVP)
Админ может: войти → добавить сервер → проверить SSH → установить Amnezia → установить AmneziaWG → смотреть job/статус сервера → создать VPN-пользователя → выдать доступ → получить конфиг → изменить срок → отозвать доступ → удалить пользователя → sync → health → смотреть audit log.
