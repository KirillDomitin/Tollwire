# frontend — веб-интерфейс панели

React 19 + Vite + TypeScript 5.9 (strict; TS 7 несовместим с typescript-eslint/openapi-typescript), React Router,
TanStack Query, openapi-fetch, свои компоненты на Radix. Node 24.

```bash
npm ci && npm run gen:api && npm run dev        # Vite :5173, /api проксируется на :8000
npm run typecheck && npm run lint && npm test && npm run build
```

Превью в Claude Desktop — `.claude/launch.json`: `api` (uvicorn :8000) и `web` (vite :5173).

## Устройство

| Путь | Что |
|---|---|
| `src/i18n/` | i18next + react-i18next, два языка: ru и en. Язык — из `localStorage` (`vpn-panel-lang`), иначе по браузеру (русский → ru, остальное → en). `format.ts` — текущий язык и кеш `Intl`-форматтеров. Смена языка пересоздаёт роутер (`LocalizedRouter` в `main.tsx`), поэтому даты и числа обновляются сразу. |
| `src/locales/{ru,en}/` | Словари по разделам: `common`, `shell`, `auth`, `jobs` (типы и статусы задач — общие), `servers`, `users`, `audit`. Ключи по смыслу: `t("shell.nav.servers")`. Типы ключей берутся из ru — опечатка ломает `tsc`. Вне компонентов — `i18n.t(...)` из `src/i18n`. |
| `src/api/client.ts` | `api` (openapi-fetch, `baseUrl = window.location.origin`), `unwrap()`, тип `Schemas`. |
| `src/api/schema.d.ts` | Генерируется: `PYTHONPATH=src uv run python -m scripts.export_openapi frontend/openapi.json` (из корня) → `npm run gen:api`. Руками не править. |
| `src/api/session.ts` | Access-токен **только в памяти**; `authFetch`: 401 → один общий refresh (single-flight) → повтор. |
| `src/auth/AuthContext.tsx` | Состояния checking/signed-out/signed-in, `can(permission)` — по `permissions` из `/auth/me`; `changePassword` (свой `fetch`, чтобы после 401 обновить токен и повторить; потом перечитывает профиль). |
| `src/auth/RequireAuth.tsx` | Пускает только вошедшего; с `must_change_password` вместо панели — `ChangePasswordPage` на том же адресе (бэкенд для такого админа отвечает 403 на всё, кроме `/auth/me`, смены пароля, refresh и logout). |
| `src/App.tsx` | Маршруты: `/login`, `/servers`, `/servers/:serverId`, `/users`, `/users/:userId`, `/audit` — каждый раздел грузится отдельным чанком (`lazy`), в основном бандле каркас, вход и словари. |
| `src/pages/server/` | Карточка сервера: правка имени и заметки (`EditServerDialog`; адрес не меняется — его смена сбрасывает ключ и доступ), чек-лист подготовки (ключ хоста, учётка, установка, протокол), обслуживание (учётка, ключ панели), мониторинг (ленивый чанк), журнал задач, опасные действия; ссылка «События сервера» (при `audit:read`) открывает журнал по серверу. |
| `src/pages/user/` | Карточка пользователя: правка имени, email и заметки (`EditUserDialog`), подписка, «Выдать доступ» (подписка → гранты → задачи выдачи), срок, отзыв, конфиг (vpn:// + серия QR + .conf). |
| `src/pages/AuditPage.tsx` | Журнал `/audit`: фильтры в URL (`admin_id`, `group`, `entity_type`, `entity_id`, `from`, `to`); подгрузка `useInfiniteQuery` по 100, курсор — `before_created_at`/`before_id` последней строки. Имена объектов — из уже загруженных списков серверов и пользователей (без права на список объект не называется). |
| `src/pages/Ledger.module.css` | Общие стили таблиц-журналов. |
| `src/pages/ChangePasswordPage.tsx` | Экран временного пароля «Задайте свой пароль» (форма + «Выйти»). Форма — `components/ChangePasswordForm` (проверки длины, совпадения и «не равен текущему» до запроса; `noValidate`, сообщения свои). «Сменить пароль» в блоке учётки — `ChangePasswordDialog`. |
| `src/components/` | Button, Dialog, ConfirmDialog (с последствиями), TextField, Notice, AppShell, ThemeSwitch, TimeChart (uPlot), ChangePasswordForm/-Dialog. |
| `src/lib/` | `access.ts` (сигнал доступа по датам), `serverSignal.ts`, `jobs.ts`, `time.ts`, `units.ts`, `theme.ts`; `audit.ts` — группа и важность событий (`AUDIT_ACTIONS`), текст события — в словаре `audit.actions.<action>` (`auditActionText`), группы — стабильные идентификаторы (`signIn`, `admins`, `servers`, `access`, `configs`: они в адресе `?group=servers`), `actionGroups`/`actionsOfGroup` для фильтра, `auditActor`, `auditSubject`, `auditDetails`. |
| `src/styles/tokens.css` | Дизайн-токены (светлая и тёмная тема). |

## Дизайн «Журнал связи»

- Палитра: `--fog`, `--paper`, `--pine` (+ `-soft`/`-faint`), сигналы `--signal-ok/warn/down/process/idle`
  (`--signal-warn-text` — для контраста AA). Шрифт Golos Text.
- Единственный акцент — цветная полоса статуса слева у строки.
- Тема — по системе + переключатель (`data-theme` на `<html>`). Цвета только через токены.
- Lighthouse «Доступность» = 100 в обеих темах — держать. Мобильная ширина без горизонтальной прокрутки.

## Правила

- Новый видимый текст — только через `t(...)`, ключ в **обоих** словарях (`i18n.test.ts` следит за совпадением ключей
  и `{{подстановок}}`). Склонения — плюральными ключами (`key_one/_few/_many/_other` в ru, `_one/_other` в en), не
  `if (n === 1)`. Даты, числа, единицы — через `Intl` с `currentLanguage()`, а не с жёстким `"ru"`.
- Тесты идут на русском: `src/test/setup.ts` перед каждым тестом включает ru. Проверка другого языка — `i18n.changeLanguage("en")`.
- Кнопки и разделы показывать по правам (`can(...)`), viewer — только чтение.
- Новое событие аудита — строка в `AUDIT_ACTIONS` (группа, тон) **и** `audit.actions.<action>` в обоих словарях (оба — `Record`/типизированные ключи по openapi: без них typecheck падает).
  В подробности выводятся только ключи из белого списка `DETAIL_LABELS` и `changes`.
- Опрос задач: каждые 2 с, пока есть pending/running; по завершении — invalidate связанных запросов.
- Конфиг: `useMutation` с `gcTime: 0`, сброс при закрытии диалога; QR и конфиги не скриншотить (в них ключ).
- uPlot рисует на canvas: цвета читать из CSS-переменных через `getComputedStyle`, перерисовка при смене темы;
  пропсы-массивы — константы или `useMemo` (иначе график пересоздаётся на каждой отрисовке).
- В отрисовке не вызывать `Date.now()` (правило чистоты React) — брать `dataUpdatedAt` из запроса.
- `qrcode` без `@types/qrcode` (npm ломает установку) — свои типы `src/types/qrcode.d.ts`.

## Тесты (vitest + Testing Library, jsdom)

- `src/test/render.tsx` — `renderWithProviders(ui, { permissions, route, path })`; `json()` — ответ для
  подменённого `fetch` (`vi.stubGlobal`).
- `src/test/setup.ts` — заглушка `matchMedia` (uPlot вызывает её при импорте); uPlot в тестах подменяется
  `vi.mock("uplot")`.

## Сборка и прод

`frontend/Dockerfile`: node:24-alpine (по дайджесту) → nginx (по дайджесту). nginx отдаёт статику, проксирует
`/api/` и `/health` (один origin, без CORS), `/assets` с кешем на год, SPA `try_files`, CSP. `npm ci` с
cache mount и повторами (сеть до npm бывает нестабильна). Основной бандл держать < 500 КБ — тяжёлое грузить
лениво (`React.lazy`).
