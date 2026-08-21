# Техническое задание

## 1. Наименование проекта

Backend-платформа для централизованного управления VPN-серверами, пользователями и VPN-доступами на базе AmneziaVPN.

Рабочее название: `VPN Control Panel`.

---

## 2. Цель проекта

Разработать backend-приложение с REST API для:

- управления VPN-серверами;
- подключения к Linux-серверам по SSH;
- автоматической установки VPN-компонентов AmneziaVPN;
- управления VPN-протоколами;
- создания и удаления VPN-пользователей;
- выдачи VPN-доступа пользователям;
- установки срока действия доступа;
- автоматического отзыва доступа после истечения срока;
- получения VPN-конфигураций;
- мониторинга состояния серверов;
- синхронизации состояния серверов с базой данных;
- ведения журнала административных действий.

Архитектура должна предусматривать дальнейшее добавление:

- платных тарифов;
- подписок;
- платежей;
- автоматического продления доступа;
- нескольких типов VPN-протоколов;
- автоматического распределения пользователей между серверами;
- Telegram-бота;
- frontend административной панели.

---

# 3. Технологический стек

Обязательные технологии:

- Python 3.10+;
- FastAPI;
- PostgreSQL;
- SQLAlchemy 2.x;
- Alembic;
- asyncio;
- Pydantic v2;
- Redis;
- AsyncSSH.

Дополнительно:

- Docker;
- Docker Compose;
- pytest;
- httpx;
- Ruff;
- mypy.

Все операции с PostgreSQL и SSH должны выполняться асинхронно.

---

# 4. Архитектурный стиль

Приложение должно использовать модульную архитектуру с разделением:

- API layer;
- application/service layer;
- domain layer;
- infrastructure layer;
- persistence layer.

Бизнес-логика не должна находиться в FastAPI routers.

API routers должны отвечать только за:

- HTTP;
- валидацию входных данных;
- авторизацию;
- вызов application services;
- формирование HTTP response.

---

# 5. Основные модули

Система должна содержать следующие модули:

```text
auth
servers
vpn_users
access
protocols
jobs
audit
```

Infrastructure:

```text
ssh
vpn
workers
```

---

# 6. Модель администратора

Система должна поддерживать пользователей административной панели.

Сущность `Admin`:

```text
id
email
password_hash
is_active
is_superuser
created_at
updated_at
last_login_at
```

Пароли должны храниться исключительно в виде Argon2id hash.

---

# 7. Аутентификация

API должно поддерживать:

```text
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout
GET  /api/v1/auth/me
```

Использовать:

- JWT access token;
- refresh token;
- HttpOnly cookie для refresh token;
- hash refresh token в PostgreSQL.

Refresh token не должен храниться в БД в открытом виде.

---

# 8. VPN-сервер

Сущность `VPNServer`:

```text
id
name
host
ssh_port
ssh_username
status
os
os_version
architecture
created_at
updated_at
last_seen_at
```

Статусы:

```text
NEW
INSTALLING
READY
OFFLINE
ERROR
```

API:

```text
GET    /api/v1/servers
POST   /api/v1/servers
GET    /api/v1/servers/{id}
PATCH  /api/v1/servers/{id}
DELETE /api/v1/servers/{id}

POST   /api/v1/servers/{id}/test-connection
POST   /api/v1/servers/{id}/install
POST   /api/v1/servers/{id}/uninstall
POST   /api/v1/servers/{id}/sync
POST   /api/v1/servers/{id}/health
```

---

# 9. SSH credentials

SSH credentials должны храниться отдельно от основной сущности сервера.

Поддержать:

```text
password
private_key
```

Секретные значения должны храниться в зашифрованном виде.

Ключ шифрования должен передаваться приложению через environment variable или Docker Secret.

Ключ шифрования запрещено хранить в PostgreSQL.

---

# 10. SSH subsystem

Необходимо реализовать абстракцию:

```python
class SSHClient:
    async def connect(...)
    async def execute(...)
    async def upload(...)
    async def download(...)
    async def close(...)
```

Должны поддерживаться:

- connection timeout;
- command timeout;
- authentication errors;
- connection errors;
- retry;
- безопасное закрытие соединения.

SSH-команды не должны формироваться посредством небезопасной конкатенации пользовательского ввода.

---

# 11. VPN Provider Architecture

Работа с VPN должна осуществляться через абстракцию провайдера.

Базовый интерфейс:

```python
class VPNProvider:
    async def install(...)
    async def uninstall(...)
    async def get_status(...)
    async def create_client(...)
    async def revoke_client(...)
    async def list_clients(...)
    async def sync(...)
```

Первичная реализация:

```text
AmneziaProvider
```

Внутри:

```text
AmneziaInstaller
AmneziaWGProvider
```

Архитектура должна позволять добавить:

```text
WireGuardProvider
XRayProvider
OpenVPNProvider
```

без изменения бизнес-логики пользователей и AccessGrant.

---

# 12. Установка Amnezia

Процесс установки должен выполняться исключительно background worker.

API не должен ожидать завершения SSH-операции.

Процесс:

```text
API request
    ↓
create Job
    ↓
worker
    ↓
SSH connection
    ↓
server validation
    ↓
Amnezia installation
    ↓
protocol installation
    ↓
verification
    ↓
database synchronization
```

---

# 13. Job system

Сущность `Job`:

```text
id
type
status
server_id
progress
message
error
created_at
started_at
finished_at
```

Статусы:

```text
PENDING
RUNNING
SUCCESS
FAILED
CANCELLED
```

Типы:

```text
INSTALL_SERVER
UNINSTALL_SERVER
INSTALL_PROTOCOL
REMOVE_PROTOCOL
CREATE_USER
DELETE_USER
REVOKE_ACCESS
SYNC_SERVER
HEALTH_CHECK
```

API:

```text
GET /api/v1/jobs
GET /api/v1/jobs/{id}
```

---

# 14. Worker

Worker должен работать отдельным процессом:

```text
vpn-admin-api
vpn-admin-worker
```

Redis используется для передачи фоновых задач.

Worker должен поддерживать asyncio.

Одновременное выполнение конфликтующих операций над одним сервером запрещается.

---

# 15. Distributed locks

Для операций над сервером необходимо использовать distributed locks.

Например:

```text
server:{id}:install
server:{id}:sync
server:{id}:protocol:{protocol_id}
```

Redis используется для реализации lock.

Lock должен иметь TTL и механизм безопасного освобождения.

---

# 16. VPN Protocol

Сущность `VPNProtocol`:

```text
id
server_id
type
name
status
config
created_at
updated_at
```

Первоначально поддержать:

```text
AMNEZIA_WG
```

Архитектура должна предусматривать:

```text
WIREGUARD
XRAY_VLESS_REALITY
OPENVPN
```

---

# 17. VPN User

VPN-пользователь является отдельной сущностью и не должен быть связан с `Admin`.

Сущность:

```text
VPNUser
```

Поля:

```text
id
username
description
status
created_at
updated_at
```

Статусы:

```text
ACTIVE
DISABLED
```

API:

```text
GET    /api/v1/vpn-users
POST   /api/v1/vpn-users
GET    /api/v1/vpn-users/{id}
PATCH  /api/v1/vpn-users/{id}
DELETE /api/v1/vpn-users/{id}
```

---

# 18. AccessGrant

Доступ VPN-пользователя к серверу должен моделироваться отдельной сущностью.

```text
AccessGrant
```

Поля:

```text
id
user_id
server_id
protocol_id
status
valid_from
valid_until
created_at
updated_at
revoked_at
```

Статусы:

```text
PENDING
ACTIVE
EXPIRED
REVOKED
```

Это необходимо для поддержки будущей системы подписок и тарифов.

---

# 19. Управление доступом

API:

```text
POST   /api/v1/vpn-users/{id}/access
GET    /api/v1/vpn-users/{id}/access

PATCH  /api/v1/access/{id}
DELETE /api/v1/access/{id}

POST   /api/v1/access/{id}/revoke
```

При выдаче доступа должны проверяться:

- существование пользователя;
- существование сервера;
- наличие протокола;
- доступность протокола;
- отсутствие конфликтующего AccessGrant.

---

# 20. Ограничение по времени

Каждый AccessGrant должен иметь:

```text
valid_from
valid_until
```

После наступления `valid_until` доступ должен переводиться в:

```text
EXPIRED
```

и VPN-доступ должен быть отозван на сервере.

Операция должна быть идемпотентной.

Повторный revoke не должен приводить к ошибке или неконсистентному состоянию.

---

# 21. VPN credentials

Конфигурация VPN-клиента должна храниться отдельно.

Секретные параметры должны храниться в зашифрованном виде.

Пример:

```text
VPNCredential
─────────────
id
access_grant_id
encrypted_config
created_at
revoked_at
```

Получение конфигурации должно быть отдельной защищенной операцией.

Приватные ключи не должны возвращаться обычными endpoint'ами списка пользователей.

---

# 22. Синхронизация

Система должна поддерживать:

```text
POST /api/v1/servers/{id}/sync
```

При синхронизации необходимо определить:

- доступность SSH;
- наличие Docker;
- наличие Amnezia;
- установленные VPN-протоколы;
- состояние VPN-контейнеров;
- доступные порты;
- существующие VPN-клиенты;
- соответствие состояния сервера данным PostgreSQL.

---

# 23. Health check

Необходимо реализовать проверку:

```text
SSH
Docker
Amnezia
VPN protocol
VPN service
```

Результат должен сохраняться в системе.

---

# 24. Redis

Redis используется для:

- background jobs;
- distributed locks;
- временного состояния;
- cache;
- rate limiting.

Redis не является основным хранилищем бизнес-данных.

PostgreSQL является source of truth.

---

# 25. Audit Log

Каждое критическое административное действие должно записываться.

Сущность:

```text
AuditLog
```

Поля:

```text
id
admin_id
action
entity_type
entity_id
metadata
ip_address
created_at
```

Логировать:

```text
LOGIN
LOGOUT

SERVER_CREATED
SERVER_UPDATED
SERVER_DELETED

SERVER_INSTALL_STARTED
SERVER_INSTALL_COMPLETED
SERVER_INSTALL_FAILED

VPN_USER_CREATED
VPN_USER_UPDATED
VPN_USER_DELETED

ACCESS_GRANTED
ACCESS_REVOKED
ACCESS_EXPIRED
```

Секретные данные не должны попадать в audit log.

---

# 26. Идемпотентность

Критические операции должны быть идемпотентными.

Особенно:

```text
server installation
protocol installation
user creation
access revoke
server sync
```

Повторное выполнение операции не должно приводить к повреждению VPN-сервера.

---

# 27. API versioning

API должен иметь версию:

```text
/api/v1/
```

Все будущие несовместимые изменения должны реализовываться через новую версию API.

---

# 28. Database

Использовать:

```text
PostgreSQL
SQLAlchemy 2.x
asyncpg
Alembic
```

Все database operations должны быть асинхронными.

Использовать:

```python
AsyncEngine
async_sessionmaker
AsyncSession
```

Миграции выполняются через Alembic.

---

# 29. Database entities

Минимальный набор:

```text
admins
admin_sessions

servers
server_credentials
vpn_protocols

vpn_users
access_grants
vpn_credentials

jobs

audit_logs
```

---

# 30. Security requirements

Система должна обеспечивать:

- HTTPS;
- Argon2id password hashing;
- JWT authentication;
- HttpOnly refresh cookies;
- encryption of SSH credentials;
- encryption of VPN credentials;
- RBAC;
- rate limiting;
- audit logging;
- input validation;
- SSH command timeout;
- protection from command injection;
- protection from SQL injection посредством SQLAlchemy;
- отсутствие секретов в логах.

---

# 31. Конфигурация

Настройки приложения должны задаваться через environment variables.

Пример:

```text
DATABASE_URL
REDIS_URL

JWT_SECRET_KEY
JWT_ACCESS_EXPIRE
JWT_REFRESH_EXPIRE

ENCRYPTION_KEY

SSH_CONNECT_TIMEOUT
SSH_COMMAND_TIMEOUT

LOG_LEVEL
```

Секреты не должны находиться в Git.

---

# 32. Docker

Production deployment должен содержать:

```text
api
worker
postgres
redis
nginx
```

API и Worker должны использовать один общий application image.

---

# 33. Logging

Использовать структурированные логи.

Каждый request должен иметь:

```text
request_id
```

SSH/job operations должны иметь:

```text
job_id
server_id
```

Запрещается логировать:

- пароли;
- SSH private keys;
- VPN private keys;
- refresh tokens;
- JWT;
- полные VPN-конфигурации.

---

# 34. Testing

Необходимо реализовать:

### Unit tests

Для:

- services;
- repositories;
- VPN providers;
- state transitions;
- expiration logic.

### Integration tests

Для:

- PostgreSQL;
- Redis;
- API;
- authentication.

### Provider tests

SSH и Amnezia provider должны иметь возможность тестироваться через mock/fake SSH layer.

---

# 35. Definition of Done для MVP

MVP считается завершенным, если администратор может:

1. Авторизоваться в API.
2. Добавить Linux-сервер.
3. Проверить SSH-соединение.
4. Запустить установку Amnezia.
5. Установить AmneziaWG.
6. Получить статус installation job.
7. Получить состояние сервера.
8. Создать VPN-пользователя.
9. Выдать ему доступ к серверу.
10. Получить VPN-конфигурацию.
11. Изменить срок действия доступа.
12. Отозвать доступ.
13. Удалить VPN-пользователя.
14. Выполнить synchronization сервера.
15. Получить health status.
16. Просмотреть audit log.

---

# 36. Будущее развитие

Архитектура должна предусматривать добавление:

```text
Tariff
Subscription
Payment
Transaction
Invoice

Notification
Telegram Bot

Multiple administrators
RBAC

Automatic server selection
Server capacity
Load balancing

Prometheus
Grafana

Multiple VPN protocols
```

Модель будущего биллинга:

```text
Tariff
   ↓
Subscription
   ↓
Payment
   ↓
AccessGrant
   ↓
VPNCredential
```

Бизнес-логика платежей не должна зависеть от конкретного VPN-протокола.

---

# 37. Архитектурное требование

Основное архитектурное правило проекта:

```text
Business Logic
       ↓
VPN Provider Interface
       ↓
Concrete Provider
       ↓
Infrastructure
       ↓
SSH / Docker / Linux
```

Бизнес-логика не должна знать:

- SSH-команды;
- Docker-команды;
- расположение конфигурационных файлов;
- внутреннюю структуру контейнеров;
- способ генерации VPN-ключей;
- особенности конкретного VPN-протокола.

Это должно быть скрыто за `VPNProvider`.