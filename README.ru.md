<div align="center">

# WirePilot

**Панель управления VPN-серверами AmneziaWG на вашем сервере.**
Добавляйте серверы, ставьте VPN по SSH, выдавайте людям доступ со сроком действия, следите за состоянием серверов и ведите журнал всех действий.

[![CI](https://github.com/KirillDomitin/Tollwire/actions/workflows/ci.yml/badge.svg)](https://github.com/KirillDomitin/Tollwire/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-2b6b4f.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab)
![Node 24](https://img.shields.io/badge/node-24-5fa04e)
![FastAPI](https://img.shields.io/badge/FastAPI-009688)
![React 19](https://img.shields.io/badge/React-19-61dafb)

[English](README.md) · Русский

</div>

---

## Зачем это нужно

Несколько VPN-серверов вручную — это SSH-сессии, пересылаемые конфиги и отсутствие записи о том, у кого к чему есть доступ. WirePilot собирает всё в одну веб-панель и один REST API:

- **Сервер подключается один раз.** Панель закрепляет его SSH-ключ хоста, ставит VPN в Docker и хранит SSH-учётку в зашифрованном виде.
- **Вы выдаёте доступ людям, а не файлы.** У человека есть подписка со сроком; клиентов на серверах панель создаёт, обновляет и отзывает сама.
- **Всегда видно, что произошло.** Каждое действие админа попадает в журнал аудита, каждая долгая операция — отслеживаемая фоновая задача.

## Возможности

| | |
|---|---|
| **Серверы** | Добавление, закрепление SSH-ключа хоста, установка VPN, проверка состояния, сверка с тем, что реально запущено. Имя, заметка и флаг страны (🇩🇪 `frankfurt-1`). |
| **VPN-пользователи и доступ** | Пользователи с подписками и сроками; доступы по серверам; мгновенный отзыв. Истёкший доступ отключается автоматически. |
| **Конфиги клиентов** | Ссылка `vpn://`, серия QR-кодов и файл `.conf`. Показ конфига — отдельное право: в нём приватный ключ клиента. |
| **Мониторинг** | CPU, память, диск, сеть и клиенты онлайн по каждому серверу, замеры по SSH (только чтение). Графики за 1 час, сутки, 7 и 30 дней; в таблице серверов — последние значения. |
| **Фоновые задачи** | Установка, выдача и отзыв конфигов, проверки и сверка выполняются воркером с очередью в Redis и блокировкой на сервер: две операции не трогают одну машину одновременно. Потерянные и зависшие задачи возвращаются автоматически. |
| **Журнал аудита** | Кто, что, с каким сервером или пользователем и когда. Фильтры по админу, группе событий, объекту и датам. |
| **Роли** | `viewer` (только чтение), `operator` (пользователи, доступы, конфиги, проверки), `superadmin` (инфраструктура и админы). Недоступные роли кнопки скрыты. |
| **Языки** | Панель на английском и русском: переключатель в боковом меню, выбор запоминается в браузере, по умолчанию — язык браузера. Даты, числа и названия стран следуют ему. |
| **Безопасность** | Секреты в базе зашифрованы (Fernet); SSH только на серверы с закреплённым ключом хоста; панель может заменить пароль своим SSH-ключом и забыть пароль; лимиты и блокировка попыток входа; ротация refresh-токенов; никакого пользовательского ввода в SSH-командах; секреты не попадают в логи. |

## Архитектура

```
 браузер ──► nginx ──► API (FastAPI) ──► PostgreSQL
               │            │
        статика React       └──► Redis ◄── воркер ──► SSH ──► ваши VPN-серверы
        (фронтенд)                       (задачи + планировщик)
```

- **API** — FastAPI, слои router → service → repository / VPN-провайдер. Протокол VPN спрятан за интерфейсом `VPNProvider`: бизнес-логика ничего не знает про SSH, Docker и форматы конфигов.
- **Воркер** — выполняет задачи из очереди и планировщик (проверки, сверка, мониторинг, истечение доступов, чистка истории).
- **Фронтенд** — React 19 + Vite + TypeScript; типизированные запросы генерируются из OpenAPI-схемы бэкенда.
- **Данные** — PostgreSQL для всего постоянного, Redis для очереди задач и лимитов.

В каждой папке `src/` лежит короткий `CLAUDE.md` с описанием модуля.

## Быстрый старт (разработка)

Нужны Docker, Python 3.12+ с [uv](https://docs.astral.sh/uv/) и Node.js 24.

```bash
git clone <адрес-репозитория> tollwire && cd tollwire
uv sync && (cd frontend && npm ci)

docker compose up -d                 # PostgreSQL + Redis
cp .env.example .env                 # затем задайте JWT_SECRET_KEY и ENCRYPTION_KEY (см. INSTALL.ru.md)
uv run alembic upgrade head
PYTHONPATH=src uv run python -m scripts.create_admin you@example.com

uv run uvicorn main:app --app-dir src --reload      # API   → http://localhost:8000
uv run python src/worker.py                          # воркер
(cd frontend && npm run dev)                         # панель → http://localhost:5173
```

Полная инструкция, включая PowerShell и частые проблемы, — в [INSTALL.ru.md](INSTALL.ru.md).

## Продакшен

Всё работает в Docker за nginx с TLS: `docker-compose.prod.yml` поднимает nginx, API, воркер, одноразовые миграции, PostgreSQL и Redis. Наружу открыт только nginx, база и Redis — во внутренней сети. Быстрее всего — `./install.sh --domain panel.example.com`: он создаст секреты, выпустит TLS-сертификат, запустит панель и предложит создать первого администратора. Подробности, сертификаты, обновление и бэкапы — в [разделе «Продакшен» INSTALL.ru.md](INSTALL.ru.md#продакшен-prod).

> **Храните `ENCRYPTION_KEY` вместе с бэкапом базы.** Им зашифрованы SSH-учётки и VPN-конфиги. Без ключа бэкап базы не прочитать.

## Разработка

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest                                # нужен `docker compose up -d`

cd frontend
npm run typecheck && npm run lint && npm test && npm run build
```

Если вы изменили API, обновите типы фронтенда:

```bash
PYTHONPATH=src uv run python -m scripts.export_openapi frontend/openapi.json
cd frontend && npm run gen:api
```

Порядок работы — в [CONTRIBUTING.md](CONTRIBUTING.md), что изменилось — в [CHANGELOG.ru.md](CHANGELOG.ru.md).

## Структура проекта

```
src/
  core/            общее: конфиг, база, безопасность, ошибки
  infrastructure/  внешний мир: ssh, VPN-провайдеры, воркеры, мониторинг
  modules/         домен: auth, servers, protocols, vpn_users, access, jobs, audit, monitoring
  scripts/         create_admin, export_openapi
alembic/           миграции базы
frontend/          панель на React
deploy/nginx/      конфиг nginx для продакшена
tests/             юнит-, сервисные и интеграционные тесты
```

## Безопасность

Нашли уязвимость? Не создавайте публичный issue — см. [SECURITY.md](SECURITY.md).

## Отказ от ответственности

WirePilot — независимый проект. Он не связан с Amnezia и проектом AmneziaVPN, не одобрен и не спонсируется ими. Панель управляет серверами, которые принадлежат вам или администрировать которые вам разрешено.

## Лицензия

[MIT](LICENSE) © 2026 Kirill Domitin
