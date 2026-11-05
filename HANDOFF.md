# Передача: обновление контейнеров протоколов

Дата: 2026-11-05. Проект: `G:/Projects/Tollwire`.

## Цель и решения

Панель проверяет доступность образа `amneziavpn/amneziawg-go:latest` ежедневно в **03:00 Europe/Moscow** и показывает «Доступно обновление» у конкретного протокола сервера. Обновление — только вручную, с сохранением клиентов, ключей, порта и конфигурации, с откатом.

- Охват: собственные AWG и шлюз; APP-контейнеры не обновлять. Установка сохраняет прежний pinned image.
- Проверка читает registry metadata и Docker inspect, без pull/stop; сравнивает config digest выбранной платформы с фактическим image ID.
- Календарный дедуп в Redis, догоняющий запуск только за текущий день. Кеш: stale после 26 ч, хранение успеха 7 дней; ошибка → unknown/stale.
- Замена — фоновая задача под ServerLock, pull до stop, immutable digest, snapshot, журнал и CID для восстановления при повторном вызове.

## Текущее состояние

Реализация, ревью, проверки, документация, живой прогон и production-деплой завершены. Ветка `codex/protocol-container-updates`, HEAD `80943d8`, отправлена в origin. Коммиты: `6561046` тесты/план, `2606e1f` провайдеры, `1990cd5` backend/расписание, `243eef3` UI/исправления ревью, `7b44bdb` название кнопки, `c530f9d` документация, `911ab5f` Docker 29, `80943d8` production-зависимость httpx. Merge в master не выполнялся.

По поручению пользователя панель на `homelab:/home/kirill/tollwire` обновлена до точного `80943d8` (detached HEAD), https://awg.domitin.com работает. API healthy, worker/nginx running, restart=0; реальные image ID совпадают с доставленными тегами релиза. Миграция `a9d1e6c42b70`, счётчики admins/servers/users/protocols сохранены: `1/6/22/9`. Backup `backups/tollwire-20261105-010527.dump`, 7095633 байта, 0600. Предыдущие образы сохранены под `tollwire-rollback/{app,web}:20261104-pre-protocol-updates`. HTTPS health/root/assets PASS, новый маршрут без авторизации возвращает 401; scheduler включён, правило protocol_updates активно. Valheim ID/StartedAt сохранены.

Пользователь возобновил работу и выбрал `homelab`. Изолированные AWG/шлюз прошли replacement/noop/rollback/recovery; выявленная ошибка определения актуальности Docker 29 исправлена и подтверждена повторным live replacement/current/noop. Тестовый стенд, производные образы и архив переноса удалены; upstream-кеш оставлен. Production VPN-контейнеры и Valheim при деплое панели не менялись. Открытых находок и обязательных шагов нет.

## Изменённые файлы

Пути ниже относительно корня; полный список фичи: `git diff master HEAD --name-only`.

| Файлы | Назначение |
|---|---|
| `src/infrastructure/vpn/{provider,config,amnezia,gateway,container_update,registry}.py`; `src/core/{exceptions,ssh_exception}.py` | Контракты, registry, замена/откат/recovery и безопасные ошибки |
| `src/modules/servers/{protocol_updates,service,schemas}.py`, `src/modules/servers/api/v1/router.py` | Кеш проверки, API и запуск обновления |
| `src/infrastructure/workers/{worker,scheduler}.py`; `src/modules/jobs/{models/job,repository,service}.py`; `src/modules/audit/models/audit_log.py` | Задачи, календарь, права запуска и аудит |
| `alembic/versions/a9d1e6c42b70_protocol_container_updates.py` | Два JobType; downgrade удаляет только историю новых типов задач |
| `frontend/src/pages/server/{ProtocolUpdates,ServerPage,JobsLog}.tsx`, `useServer.ts`, `ServerPage.module.css` | Статусы протоколов, подтверждение обновления, результаты/откат |
| `frontend/{openapi.json,src/api/schema.d.ts}`, `frontend/src/lib/audit.ts`, `frontend/src/locales/{ru,en}/{servers,jobs,audit}.json` | Типы API и переводы |
| `tests/fakes/{fake_ssh,protocol_update}.py`; `tests/unit/test_protocol_{update,update_discovery,registry_security}.py`; `tests/services/test_protocol_{update_checks,update_migration,registry_cache}.py`; `tests/integration/test_protocol_update{,_checks_api,_schedule}.py` | Провайдер, registry, кеш, календарь, API, миграции |
| `frontend/src/pages/server/{ProtocolUpdate,JobsLog.protocolUpdates}.test.tsx` | UI и результаты задач |
| `docs/servers/protocol-updates.md`, `docs/tests/protocol-updates.md`, `docs/plans/protocol-container-updates.md` | Основные контракт, проверки и план |
| `docs/servers/module-reference.md`, `docs/vpn/{provider,files}.md`, `docs/frontend/structure.md`; модульные `CLAUDE.md` VPN/workers/servers/jobs/tests, `frontend/AGENTS.md`; `CHANGELOG{,.ru}.md` | Справочники и инварианты |

## Проверки

- Полный backend через test-runner: **3386 passed, 355 skipped**, 516.27 с; включены slow, миграции и скрипты. Пропуски — ограничения Windows/POSIX.
- Полный frontend: **894 passed**. Ruff, mypy (197 файлов), frontend typecheck/lint/build — PASS.
- Upgrade/downgrade проверены, включая старые данные задач. Повторное ревью без находок.
- Реальный registry probe: amd64 metadata OK; arm64/v8 → platform_unknown. Только публичные metadata, без SSH/pull; это не доказательство живой замены контейнера.
- После Docker 29 fix: RED 10 ожидаемых падений, 38 passed → GREEN 48 passed; затронутая регрессия 609 passed (один shell-тест повторён через Git Bash). Ruff/mypy 197 файлов и повторное ревью — PASS; весь backend повторять оснований не было.
- Homelab Docker 29.6.1: реальные AWG/GW замена, сохранность, noop, откат, rename recovery, verified cleanup после добавления клиента — PASS. После fix повторены replacement/current/noop. Выходы/назначения/RU сохранены; пакет через gwx1 прошёл с временным тестовым маршрутом. Полный клиентский туннель в интернет не проверялся; different-index/same-child noop покрыт unit-тестами, живой alias отсутствовал.

## Неудачные попытки и исправления

- Первый production-деплой 911ab5f выявил ModuleNotFoundError: httpx был только в dev. Панель штатно возвращена на d540b1d с downgrade миграции, без восстановления БД/потери данных. Исправлено 80943d8: существующая версия httpx перенесена в runtime, lock без обновления версий. 21 focused tests, lock check, reviewer PASS. В реальном production Docker-образе без сети и dev-пакетов импорт main/worker и OpenAPI-маршруты PASS, затем повторный деплой успешен.
- Docker был выключен → ConnectionRefusedError PostgreSQL до assertions; запускались только локальные `tollwire-postgres-1`/`tollwire-redis-1`.
- WSL Bash E_ACCESSDENIED → shell-тесты проходили с Git Bash, cwd `C:/Program Files/Git/bin`, абсолютными путями pytest. Для xdist нужен собственный доступный basetemp/cache.
- Первые fake SSH/registry fixtures отставали от команд и реальных digest → фейки синхронизированы, положительные metadata используют SHA тела; защиту production-клиента не ослабляли.
- Worker UnboundLocalError server_id → оба новых JobType добавлены в валидацию серверных задач.
- Ревью P2: verified cleanup не должен требовать старых peers после добавления клиента; несколько RepoDigests одного image ID допустимы. Исправлено и покрыто регрессиями.
- Две кнопки «Проверить сейчас» сломали Gateway-тест → новая кнопка названа «Проверить обновления»; полный frontend повторно зелёный.
- На homelab загрузка слоёв Docker Hub обрывалась TCP read timeout. Образ перенесён из локального Docker через SSH, SHA256 архива сверен; canonical pull после импорта прошёл. DNS/daemon не меняли.
- Docker 29/containerd возвращает index в `.Image`/`.Id`, а не config ID: после замены возникал ложный available. Добавлены optional platform descriptor и trusted installed-manifest→config resolution; публичные/raw journal ID сохранены, unknown вместо угадывания при недоступном manifest. Updater пропускает замену при равных platform manifests.

## Следующие шаги и ограничения

1. Работа опубликована и развёрнута; обязательных шагов нет. Merge в master — только по поручению.
2. При продолжении прочитать актуальные `AGENTS.md` и план; успешные проверки не повторять без изменений/нового риска.
3. Сторонние локальные изменения ниже сохранены и не включены в коммиты фичи.

Не читать `.env*` или реальные ключи; не логировать конфиги/токены. Локальный pytest — через test-runner; его роль запрещает реальные серверы, живой стенд исполнял worker. Этот scratch-файл **не коммитить**.

При создании handoff в рабочем дереве обнаружены сторонние изменения `.claude/agents/{doc-writer,reviewer}.md`, `.claude/skills/{feature,quick}/SKILL.md`, `.claude/settings.json`, `CLAUDE.md`, `frontend/{AGENTS,CLAUDE}.md`; untracked `.codex/`, `AGENTS.md`, `docs/frontend/agent-reference.md`. Не откатывать и не включать их в коммиты фичи автоматически.

## Память

claude-mem observer не сохраняет память из-за отказа credentials/account verification. На автоматическую timeline не полагаться; источник состояния — этот файл, план, git и закрытый work-state `protocol-container-updates` (реализация завершена).
