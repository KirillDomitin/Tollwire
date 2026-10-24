# Тесты: подробный перечень по слоям

Перенесено из `tests/CLAUDE.md` без изменений текста; там остались окружение, правила и основа фейка SSH.

## Слои

| Папка | Что |
|---|---|
| `unit/` | Чистые функции: awg_config (в т.ч. `test_awg_discovery.py` — clientsTable и peer'ы чужой установки), amnezia_link, host_metrics, host_keys, authorized_keys (текст файла), логи, правила, провайдеры на фейке (`test_provider.py`; `test_amnezia_app.py` — только чтение приложения AmneziaVPN, `test_amnezia_app_write.py` — запись (отзыв,
`restore_peer`, протокол записи), `test_import_read.py` — `read_importable` для импорта (только чтение, PSK не утекает), `test_fake_amnezia_app.py` и `test_fake_amnezia_app_write.py` — страховка фейка); `test_ssh_real_server.py` — настоящий AsyncSSH-сервер в процессе. |
| `services/` | Сервисы с настоящей БД и Redis: доступы, sync/health (в том числе возврат peer'ов импортированных клиентов — `test_sync_imported.py`), ключ хоста, ключ панели, миграции (upgrade/downgrade, в том числе миграция импорта 23.4 с данными нового вида), мониторинг. |
| `integration/` | Через HTTP (`httpx.ASGITransport`, `base_url=http://testserver` для cookie), настоящий login, RBAC-матрица по всем маршрутам, сценарий DoD целиком (`test_mvp_flow.py`), воркер через `process_next()`/`drain(worker)`, установка SSH-ключа панели, поиск Amnezia (`test_discovery_job.py` — задача и автопоиск после установки, `test_discovery_api.py` — API и статусы клиентов; общее — `discovery_support.py`), отзыв клиента приложения AmneziaVPN (`test_app_revoke.py`: строки протокола и credential
в БД создаются вручную). Импорт клиентов приложения в БД панели (этап 23.4): `test_import_api.py` (POST /servers/{id}/import: права,
422/404/409, params задачи), `test_import_job.py` (воркер: пользователь/подписка/грант/credential, повтор, gone/unsupported/
already/conflict, savepoint на клиента, аудит, секреты), `test_import_lifecycle.py` (отзыв и истечение импортированного
клиента, 409 на конфиг, `source`/`external_name` в API, пользователи без email, `DELETE /protocol` не трогает приложение);
общее — `tests/import_support.py` (сцена «сервер с приложением», импортированный credential в БД). |

Бот (перечень — `docs/bot/tests.md`): `unit/test_bot*.py`, `services/test_bot_*.py`, `integration/test_bot_*.py`; миграция бота — `services/test_migrations.py`; права бота — `integration/test_rbac.py`.
