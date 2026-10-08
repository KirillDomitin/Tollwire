# frontend — веб-интерфейс панели

React 19 + Vite + TypeScript 5.9 (strict; TS 7 несовместим с typescript-eslint/openapi-typescript), React Router,
TanStack Query, openapi-fetch, свои компоненты на Radix. Node 24.

```bash
npm ci && npm run gen:api && npm run dev        # Vite :5173, /api проксируется на :8000
npm run typecheck && npm run lint && npm test && npm run build
```

## Устройство

| Путь | Что |
|---|---|
| `src/api/client.ts` | `api` (openapi-fetch, `baseUrl = window.location.origin`), `unwrap()`, тип `Schemas`. |
| `src/api/schema.d.ts` | Генерируется: `PYTHONPATH=src uv run python -m scripts.export_openapi frontend/openapi.json` (из корня) → `npm run gen:api`. Руками не править. |
| `src/api/session.ts` | Access-токен **только в памяти**; `authFetch`: 401 → один общий refresh (single-flight) → повтор. |
| `src/auth/AuthContext.tsx` | Состояния checking/signed-out/signed-in, `can(permission)` — по `permissions` из `/auth/me`. |
| `src/App.tsx` | Маршруты: `/login`, `/servers`, `/servers/:serverId`, `/users`, `/users/:userId`. |
| `src/pages/server/` | Карточка сервера: чек-лист подготовки, обслуживание, мониторинг (ленивый чанк), журнал задач, опасные действия. |
| `src/pages/user/` | Карточка пользователя: подписка, «Выдать доступ» (подписка → гранты → задачи выдачи), срок, отзыв, конфиг (vpn:// + серия QR + .conf). |
| `src/pages/Ledger.module.css` | Общие стили таблиц-журналов. |
| `src/components/` | Button, Dialog, ConfirmDialog (с последствиями), TextField, Notice, AppShell, ThemeSwitch, TimeChart (uPlot). |
| `src/lib/` | `access.ts` (сигнал доступа по датам), `serverSignal.ts`, `jobs.ts`, `time.ts`, `units.ts`, `theme.ts`; `audit.ts` — заготовка 20.4, не закоммичена. |
| `src/styles/tokens.css` | Дизайн-токены (светлая и тёмная тема). |

## Дизайн «Журнал связи»

- Палитра: `--fog`, `--paper`, `--pine` (+ `-soft`/`-faint`), сигналы `--signal-ok/warn/down/process/idle`
  (`--signal-warn-text` — для контраста AA). Шрифт Golos Text.
- Единственный акцент — цветная полоса статуса слева у строки.
- Тема — по системе + переключатель (`data-theme` на `<html>`). Цвета только через токены.
- Lighthouse «Доступность» = 100 в обеих темах — держать. Мобильная ширина без горизонтальной прокрутки.

## Правила

- Кнопки и разделы показывать по правам (`can(...)`), viewer — только чтение.
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
