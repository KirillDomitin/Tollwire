import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";

import { router } from "../../App";
import { setAccessToken } from "../../api/session";
import {
  NOTIFY,
  NOTIFY_CODE,
  NOTIFY_LINK,
  NOTIFY_OUTBOX_ID,
  OPERATOR,
  SECRET_DETAIL,
  VIEWER,
  botStatus,
  cachedText,
  conflict,
  linkedTelegram,
  mockBot,
  myTelegram,
  tooMany,
  ago,
  type BotBackend,
} from "../../test/botApi";
import { json, renderWithProviders } from "../../test/render";
import { NotifyPage } from "./NotifyPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.5, вкладка «Мои уведомления»). Тексты ru из плана; если реализация
 * назовёт иначе, правятся константы ниже, а не десяток тестов.
 * - `src/pages/bot/NotifyPage.tsx` экспортирует `NotifyPage` (маршрут /bot/notify, ленивый чанк, ссылка «Мои
 *   уведомления» в навигации раздела - только при праве `bot:notify`).
 * - Не привязан: «Telegram не привязан» и кнопка «Привязать» -> POST /me/telegram/code, окно (role=dialog) со ссылкой
 *   «Открыть в Telegram» (href = link из ответа), кодом, обратным отсчётом `м:сс` и кнопкой «Закрыть». Пока окно открыто,
 *   GET /me/telegram опрашивается раз в 2 с до `linked`; тогда окно закрывается. Код нигде не остаётся (gcTime: 0).
 * - Привязан: «@имя», время привязки, кнопки «Проверить» (POST /me/telegram/test, затем опрос GET /outbox/{id}:
 *   «Сообщение отправлено») и «Отвязать» (ConfirmDialog -> DELETE /me/telegram).
 * - События: чекбоксы с названиями из каталога (ключ -> название ниже), изменение сразу уходит PUT /me/telegram/events
 *   с полным объектом `{events: {ключ: bool}}`.
 * - Предупреждения: `notify_capable: false` - «обновите бота»; `last_seen_local: true` - подсказка «на том же сервере»;
 *   `blocked_at` - «заблокировал бота». Свёрнутый список «Кто получает» (GET /admin-links, без telegram_id).
 * - Ошибки - Notice без `ApiError.detail`.
 */

const ME = "/api/v1/bot-admin/me/telegram";
const EVENTS_NAME = {
  server_down: /^Сервер недоступен/,
  server_recovered: /^Сервер снова доступен/,
  jobs_failed: /^Сбои задач/,
  login_locked: /Блокировка входа/,
  panel_down: /^Панель недоступна/,
} as const;
const ACCESS = ["servers:read", "vpn_users:read"];
const POLL = { timeout: 5000 };

function setup(permissions: string[] = [...OPERATOR, NOTIFY], backend: BotBackend = {}) {
  const api = mockBot({ permissions, ...backend });
  const rendered = renderWithProviders(<NotifyPage />, { permissions });
  return { api, ...rendered };
}

function renderApp(location: string, backend: BotBackend = {}) {
  const api = mockBot(backend);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const memory = createMemoryRouter(router.routes, { initialEntries: [location] });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={memory} />
    </QueryClientProvider>,
  );
  return { api, memory };
}

beforeEach(() => {
  setAccessToken(null);
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const linkedAdmin = (overrides: Record<string, unknown> = {}): BotBackend => ({
  me: linkedTelegram(overrides),
});
const checkbox = (name: RegExp) => screen.findByRole("checkbox", { name });

describe("notify tab: navigation and rights", () => {
  it("is in the bot section navigation for an admin with bot:notify", async () => {
    renderApp("/bot", { permissions: [...ACCESS, "bot:read", NOTIFY] });

    expect(await screen.findByRole("link", { name: "Мои уведомления" })).toHaveAttribute(
      "href",
      "/bot/notify",
    );
  });

  it("is not in the navigation without bot:notify", async () => {
    renderApp("/bot", { permissions: [...ACCESS, ...VIEWER] });

    await screen.findByRole("link", { name: "Состояние" });
    expect(screen.queryByRole("link", { name: "Мои уведомления" })).not.toBeInTheDocument();
  });

  it("opens by its address and marks itself as the open section", async () => {
    renderApp("/bot/notify", { permissions: [...ACCESS, ...VIEWER, NOTIFY] });

    expect(await screen.findByRole("link", { name: "Мои уведомления" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(await screen.findByText("Telegram не привязан")).toBeInTheDocument();
  });

  it("lets a viewer with bot:notify link, choose events and send himself a check", async () => {
    setup([...VIEWER, NOTIFY], linkedAdmin());

    expect(await screen.findByRole("button", { name: "Проверить" })).toBeEnabled();
    expect(await checkbox(EVENTS_NAME.server_down)).toBeEnabled();
    expect(screen.getByRole("button", { name: "Отвязать" })).toBeEnabled();
  });
});

describe("notify tab: not linked", () => {
  it("says the Telegram is not linked and offers to link it", async () => {
    setup();

    expect(await screen.findByText("Telegram не привязан")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Привязать" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отвязать" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Проверить" })).not.toBeInTheDocument();
  });

  it("asks for a code and shows the link, the code and the countdown in a dialog", async () => {
    const user = userEvent.setup();
    const { api } = setup();

    await user.click(await screen.findByRole("button", { name: "Привязать" }));

    const dialog = await screen.findByRole("dialog");
    expect(api.count("POST", `${ME}/code`)).toBe(1);
    expect(within(dialog).getByRole("link", { name: /Открыть в Telegram/i })).toHaveAttribute(
      "href",
      NOTIFY_LINK,
    );
    expect(within(dialog).getByText(NOTIFY_CODE)).toBeInTheDocument();
    expect(within(dialog).getByText(/\d{1,2}:\d{2}/)).toBeInTheDocument();
  });

  it("closes the dialog and shows the Telegram once the bot has linked it (polls every two seconds)", async () => {
    const user = userEvent.setup();
    const { api, queryClient } = setup();
    await user.click(await screen.findByRole("button", { name: "Привязать" }));
    await screen.findByRole("dialog");
    const before = api.count("GET", ME);

    api.world.me = linkedTelegram();

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument(), POLL);
    expect(api.count("GET", ME)).toBeGreaterThan(before);
    expect(await screen.findByText(/@admin_tg/)).toBeInTheDocument();
    await waitFor(() => expect(cachedText(queryClient)).not.toContain(NOTIFY_CODE));
    expect(cachedText(queryClient)).not.toContain(NOTIFY_LINK);
  });

  it("clears the linking secret from the cache when the page unmounts", async () => {
    const user = userEvent.setup();
    const { queryClient, unmount } = setup();
    await user.click(await screen.findByRole("button", { name: "Привязать" }));
    await screen.findByText(NOTIFY_CODE);

    unmount();

    await waitFor(() => expect(cachedText(queryClient)).not.toContain(NOTIFY_CODE));
    expect(cachedText(queryClient)).not.toContain(NOTIFY_LINK);
  });

  it("keeps a blocked relink open and polls until the new binding clears the block", async () => {
    const user = userEvent.setup();
    const { api, queryClient } = setup(undefined, linkedAdmin({ blocked_at: ago(600) }));
    await user.click(await screen.findByRole("button", { name: "Привязать" }));
    await screen.findByText(NOTIFY_CODE);
    const before = api.count("GET", ME);

    await waitFor(() => expect(api.count("GET", ME)).toBeGreaterThan(before), POLL);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    api.world.me = linkedTelegram({ telegram_username: "new_admin" });

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument(), POLL);
    expect(await screen.findByText("@new_admin")).toBeInTheDocument();
    await waitFor(() => expect(cachedText(queryClient)).not.toContain(NOTIFY_CODE));
  });

  it("hides an expired code, never shows negative time and stops polling", async () => {
    const user = userEvent.setup();
    const { api } = setup(undefined, {
      respond: {
        [`POST ${ME}/code`]: () => json({ code: NOTIFY_CODE, link: NOTIFY_LINK, expires_at: ago(1) }, 201),
      },
    });
    await user.click(await screen.findByRole("button", { name: "Привязать" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/истёк/i);
    expect(within(dialog).queryByText(NOTIFY_CODE)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("link")).not.toBeInTheDocument();
    expect(dialog.textContent).not.toMatch(/-\d+:\d+/);
    const before = api.count("GET", ME);

    await new Promise((resolve) => setTimeout(resolve, 2600));

    expect(api.count("GET", ME)).toBe(before);
  });

  it("does not poll after the dialog is closed", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await user.click(await screen.findByRole("button", { name: "Привязать" }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "Закрыть" }));
    const after = api.count("GET", ME);

    await new Promise((resolve) => setTimeout(resolve, 2600));

    expect(api.count("GET", ME)).toBe(after);
  });

  it("leaves the code neither on the screen nor in the query cache after the dialog is closed", async () => {
    const user = userEvent.setup();
    const { queryClient } = setup();
    await user.click(await screen.findByRole("button", { name: "Привязать" }));
    await screen.findByText(NOTIFY_CODE);

    await user.click(screen.getByRole("button", { name: "Закрыть" }));

    await waitFor(() => expect(screen.queryByText(NOTIFY_CODE)).not.toBeInTheDocument());
    expect(cachedText(queryClient)).not.toContain(NOTIFY_CODE);
  });

  it("shows no backend detail when the code cannot be issued", async () => {
    const user = userEvent.setup();
    setup(undefined, { respond: { [`POST ${ME}/code`]: () => json({ detail: SECRET_DETAIL }, 500) } });

    await user.click(await screen.findByRole("button", { name: "Привязать" }));

    expect(await screen.findByRole("alert")).not.toHaveTextContent(SECRET_DETAIL);
  });

  it("says when to try again after the limit of codes (429)", async () => {
    const user = userEvent.setup();
    setup(undefined, { respond: { [`POST ${ME}/code`]: () => tooMany(42) } });

    await user.click(await screen.findByRole("button", { name: "Привязать" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/42/);
  });
});

describe("notify tab: linked", () => {
  it("shows the Telegram name and the buttons", async () => {
    setup(undefined, linkedAdmin());

    expect(await screen.findByText(/@admin_tg/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Проверить" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отвязать" })).toBeInTheDocument();
    expect(screen.queryByText("Telegram не привязан")).not.toBeInTheDocument();
  });

  it("never shows a telegram id", async () => {
    setup(undefined, {
      ...linkedAdmin(),
      adminLinks: [{ admin_email: "admin@example.com", telegram_username: "admin_tg" }],
    });

    await screen.findByText(/@admin_tg/);

    expect(document.body.textContent).not.toMatch(/\d{9,}/);
  });

  it("sends a check, polls the message and reports that it was sent", async () => {
    const user = userEvent.setup();
    const { api } = setup(undefined, linkedAdmin());

    await user.click(await screen.findByRole("button", { name: "Проверить" }));

    expect(await screen.findByText(/сообщение отправлено/i, undefined, POLL)).toBeInTheDocument();
    expect(api.count("POST", `${ME}/test`)).toBe(1);
    expect(api.count("GET", `/api/v1/bot-admin/outbox/${NOTIFY_OUTBOX_ID}`)).toBeGreaterThanOrEqual(1);
  });

  it("reports that the chat is blocked without showing the backend detail (409 bot_blocked)", async () => {
    const user = userEvent.setup();
    setup(undefined, { ...linkedAdmin(), respond: { [`POST ${ME}/test`]: () => conflict("bot_blocked") } });

    await user.click(await screen.findByRole("button", { name: "Проверить" }));

    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent(SECRET_DETAIL);
    expect(alert).toHaveTextContent(/заблокир/i);
  });

  it("asks to link again when the panel says the Telegram is not linked (409 not_linked)", async () => {
    const user = userEvent.setup();
    setup(undefined, { ...linkedAdmin(), respond: { [`POST ${ME}/test`]: () => conflict("not_linked") } });

    await user.click(await screen.findByRole("button", { name: "Проверить" }));

    expect(await screen.findByRole("alert")).not.toHaveTextContent(SECRET_DETAIL);
  });

  it("asks for confirmation before unlinking and sends nothing on cancel", async () => {
    const user = userEvent.setup();
    const { api } = setup(undefined, linkedAdmin());

    await user.click(await screen.findByRole("button", { name: "Отвязать" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /Отмена|Не отвязывать/ }));

    expect(api.count("DELETE", ME)).toBe(0);
  });

  it("unlinks after the confirmation and goes back to the not linked state", async () => {
    const user = userEvent.setup();
    const { api } = setup(undefined, linkedAdmin());

    await user.click(await screen.findByRole("button", { name: "Отвязать" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Отвязать" }));

    await waitFor(() => expect(api.count("DELETE", ME)).toBe(1));
    expect(await screen.findByText("Telegram не привязан")).toBeInTheDocument();
  });
});

describe("notify tab: events", () => {
  it("shows the events by the state the panel gave", async () => {
    setup(undefined, linkedAdmin());

    expect(await checkbox(EVENTS_NAME.server_down)).toBeChecked();
    expect(await checkbox(EVENTS_NAME.panel_down)).toBeChecked();
    expect(await checkbox(EVENTS_NAME.login_locked)).not.toBeChecked();
  });

  it("sends the whole set of events when one is switched on", async () => {
    const user = userEvent.setup();
    const { api } = setup(undefined, linkedAdmin());

    await user.click(await checkbox(EVENTS_NAME.login_locked));

    await waitFor(() => expect(api.count("PUT", `${ME}/events`)).toBe(1));
    const body = api.last("PUT", `${ME}/events`)?.body as { events: Record<string, boolean> };
    expect(body.events.login_locked).toBe(true);
    expect(body.events.server_down).toBe(true);
    expect(Object.keys(body.events)).toHaveLength(10);
  });

  it("sends the whole set of events when one is switched off", async () => {
    const user = userEvent.setup();
    const { api } = setup(undefined, linkedAdmin());

    await user.click(await checkbox(EVENTS_NAME.jobs_failed));

    await waitFor(() => expect(api.count("PUT", `${ME}/events`)).toBe(1));
    expect(
      (api.last("PUT", `${ME}/events`)?.body as { events: Record<string, boolean> }).events.jobs_failed,
    ).toBe(false);
  });

  it("puts the events back when the panel refuses and shows no detail", async () => {
    const user = userEvent.setup();
    setup(undefined, {
      ...linkedAdmin(),
      respond: { [`PUT ${ME}/events`]: () => json({ detail: SECRET_DETAIL }, 422) },
    });

    await user.click(await checkbox(EVENTS_NAME.login_locked));

    expect(await screen.findByRole("alert")).not.toHaveTextContent(SECRET_DETAIL);
    await waitFor(async () => expect(await checkbox(EVENTS_NAME.login_locked)).not.toBeChecked());
  });

  it("keeps a successful event change when refetch fails and preserves it in the next toggle", async () => {
    const user = userEvent.setup();
    const { api } = setup(undefined, {
      ...linkedAdmin(),
      respond: { [`GET ${ME}`]: (_call, n) => n === 0 ? json(linkedTelegram()) : json({ detail: SECRET_DETAIL }, 500) },
    });

    await user.click(await checkbox(EVENTS_NAME.login_locked));
    await waitFor(async () => expect(await checkbox(EVENTS_NAME.jobs_failed)).toBeEnabled());
    expect(await checkbox(EVENTS_NAME.login_locked)).toBeChecked();
    await user.click(await checkbox(EVENTS_NAME.jobs_failed));

    await waitFor(() => expect(api.count("PUT", `${ME}/events`)).toBe(2));
    expect((api.last("PUT", `${ME}/events`)?.body as { events: Record<string, boolean> }).events).toMatchObject({
      login_locked: true, jobs_failed: false,
    });
  });

  it("lets the events be chosen before the Telegram is linked", async () => {
    setup(undefined, { me: myTelegram() });

    expect(await checkbox(EVENTS_NAME.server_down)).toBeInTheDocument();
  });
});

describe("notify tab: warnings", () => {
  it("shows none when everything is fine", async () => {
    setup(undefined, linkedAdmin());

    await screen.findByText(/@admin_tg/);

    expect(screen.queryByText(/обновите бота/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/на том же сервере/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/заблокировал бота/i)).not.toBeInTheDocument();
  });

  it("asks to update an old bot that cannot deliver notifications", async () => {
    setup(undefined, {
      ...linkedAdmin(),
      status: botStatus({ notify_capable: false, last_seen_version: "0.2.0" }),
    });

    expect(await screen.findByText(/обновите бота/i)).toBeInTheDocument();
  });

  it("warns that a bot on the panel server cannot report the host going down", async () => {
    setup(undefined, { ...linkedAdmin(), status: botStatus({ last_seen_local: true }) });

    expect(await screen.findByText(/на том же сервере/i)).toBeInTheDocument();
  });

  it("tells that the person blocked the bot and offers to link again", async () => {
    setup(undefined, linkedAdmin({ blocked_at: ago(600) }));

    expect(await screen.findByText(/заблокировал бота/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Привязать" })).toBeInTheDocument();
  });
});

describe("notify tab: who receives", () => {
  it("keeps the list folded and shows the admins on request, without telegram ids", async () => {
    const user = userEvent.setup();
    setup(undefined, {
      ...linkedAdmin(),
      adminLinks: [
        { admin_email: "anna@example.com", telegram_username: "anna_tg" },
        { admin_email: "boris@example.com", telegram_username: null },
      ],
    });
    await screen.findByText(/@admin_tg/);
    const folded = screen.queryByText("anna@example.com");
    if (folded) expect(folded).not.toBeVisible();

    await user.click(screen.getByText("Кто получает"));

    expect(await screen.findByText("anna@example.com")).toBeVisible();
    expect(screen.getByText("boris@example.com")).toBeVisible();
  });
});

describe("notify tab: loading and failures", () => {
  it("shows no backend detail when the state cannot be read", async () => {
    setup(undefined, { respond: { [`GET ${ME}`]: () => json({ detail: SECRET_DETAIL }, 500) } });

    expect(await screen.findByRole("alert")).not.toHaveTextContent(SECRET_DETAIL);
  });

  it("shows that it is loading while the state is awaited", async () => {
    setup(undefined, { respond: { [`GET ${ME}`]: () => new Promise<Response>(() => {}) } });

    expect(await screen.findByText(/загрузка/i)).toBeInTheDocument();
  });
});
