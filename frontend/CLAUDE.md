# frontend — веб-интерфейс панели

React 19 + Vite + TypeScript 5.9 (strict; TS 7 несовместим с typescript-eslint/openapi-typescript), React Router,
TanStack Query, openapi-fetch, свои компоненты на Radix. Node 24.

```bash
npm ci && npm run gen:api && npm run dev        # Vite :5173, /api проксируется на :8000
npm run typecheck && npm run lint && npm test && npm run build
```

Превью в Claude Desktop — `.claude/launch.json`: `api` (uvicorn :8000) и `web` (vite :5173).

## Устройство (коротко; подробно по каждой строке — `docs/frontend/structure.md`, читать перед правкой этой части)

| Путь | Что |
|---|---|
| `src/i18n/`, `src/locales/{ru,en}/` | i18next, ru и en; словари по разделам. Типы ключей из ru — опечатка ломает `tsc`. Вне компонентов — `i18n.t(...)`. |
| `src/api/client.ts` | `api` (openapi-fetch), `unwrap()`, `Schemas`. `ApiError.detail` — английский текст для выбора сообщения, **показывать нельзя**. |
| `src/api/schema.d.ts` | Генерируется (`export_openapi` → `npm run gen:api`), руками не править; CI сверяет. |
| `src/api/session.ts` | Access-токен **только в памяти**; 401 → один общий refresh → повтор. |
| `src/auth/` | `AuthContext` (`can(permission)`, `changePassword`), `RequireAuth` (с `must_change_password` — экран смены пароля). |
| `src/App.tsx` | Маршруты `/login`, `/servers`, `/servers/:serverId`, `/users`, `/users/:userId`, `/audit`; разделы — ленивые чанки. |
| `src/pages/server/` | Карточка сервера: чек-лист, обслуживание, мониторинг, журнал задач, найденная AmneziaVPN и перенос клиентов (`ImportClientsDialog`, `discovery.ts`). «Свой» протокол — только `amnezia_wg`; шлюз — отдельный протокол, в `protocol` не попадает. |
| `src/pages/server/gateway/` | Раздел «Шлюз»: `ExitsPanel`, `AssignmentsPanel`, `RuPanel`, диалоги; данные `keys.gateway*`/`geoipKeys` (`useServer.ts`); фразы бэкенда переводит `geoipErrorText`. |
| `src/pages/user/` | Карточка пользователя: подписка, доступы (`GrantRow`, `GrantAccessDialog`), конфиг. `email` может быть `null`; у `source: "imported"` конфига нет. |
| `src/pages/AuditPage.tsx` | Журнал `/audit`: фильтры в URL, `useInfiniteQuery` по 100, курсор `before_created_at`/`before_id`. |
| `src/components/` | Button, Dialog, ConfirmDialog (`NeedsConfirmation` → повтор с `again = true`), TextField, Notice, AppShell, TimeChart (uPlot) и др. |
| `src/lib/` | `audit.ts` (`AUDIT_ACTIONS`, группы, белый список подробностей), `access.ts`, `jobs.ts`, `time.ts`, `units.ts`, `theme.ts`. |
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

- `src/test/render.tsx` — `renderWithProviders(ui, { permissions, route, path })` (возвращает и `queryClient` — проверить, какие запросы помечены устаревшими); `json()` — ответ для
  подменённого `fetch` (`vi.stubGlobal`).
- `src/test/gatewayApi.ts` — подменный бэкенд шлюза (`mockGateway`: выходы, назначения, geoip, исключения, задачи, имена клиентов; все запросы и тела запоминаются) для тестов раздела «Шлюз».
- `src/test/setup.ts` — заглушка `matchMedia` (uPlot вызывает её при импорте); uPlot в тестах подменяется
  `vi.mock("uplot")`.

## Сборка и прод

`frontend/Dockerfile`: node:24-alpine (по дайджесту) → nginx (по дайджесту). nginx отдаёт статику, проксирует
`/api/` и `/health` (один origin, без CORS), `/assets` с кешем на год, SPA `try_files`, CSP. `npm ci` с
cache mount и повторами (сеть до npm бывает нестабильна). Основной бандл держать < 500 КБ — тяжёлое грузить
лениво (`React.lazy`).
