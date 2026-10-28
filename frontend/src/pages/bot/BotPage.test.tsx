import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";

import { router } from "../../App";
import { setAccessToken } from "../../api/session";
import i18n from "../../i18n";
import {
  botStatus,
  mockBot,
  SECRET_DETAIL,
  SUPERADMIN,
  OPERATOR,
  VIEWER,
  type BotBackend,
} from "../../test/botApi";
import { json } from "../../test/render";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4). Здесь экран открывается через настоящие маршруты `router` из
 * src/App.tsx и настоящий AuthProvider (права приходят из GET /auth/me), поэтому имена компонентов не важны.
 * - Маршруты внутри AppShell: /bot (состояние и ключи), /bot/invites, /bot/registrations, /bot/broadcasts,
 *   /bot/settings. Каждая страница - ленивый чанк (у листа маршрута есть `lazy`).
 * - Английские названия разделов: «Status», «Invites», «Registrations», «Broadcasts», «Settings»; заголовок «Bot».
 * - Каркас раздела (src/pages/bot/BotPage.tsx, `BotPage`) - заголовок «Бот», навигация из пяти ссылок
 *   «Состояние», «Приглашения», «Регистрации», «Рассылки», «Настройки» (NavLink: aria-current="page" у открытой;
 *   «Состояние» - с `end`) и <Outlet />.
 * - Каркас сам читает GET /api/v1/bot-admin/status. Пока ответа нет - «Загрузка…» (aria-busy). Нет ключей и нет права
 *   bot:keys - вместо разделов «Бот не подключён» (без навигации). Ошибка запроса - Notice без `ApiError.detail`.
 */

function renderApp(location: string, backend: BotBackend = {}) {
  const api = mockBot(backend);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const memory = createMemoryRouter(router.routes, { initialEntries: [location] });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={memory} />
    </QueryClientProvider>,
  );
  return { api, memory, queryClient };
}

const ACCESS = ["servers:read", "vpn_users:read"];
const SECTIONS = [
  ["/bot", "Состояние"],
  ["/bot/invites", "Приглашения"],
  ["/bot/registrations", "Регистрации"],
  ["/bot/broadcasts", "Рассылки"],
  ["/bot/settings", "Настройки"],
] as const;

beforeEach(() => {
  setAccessToken(null);
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bot section: navigation", () => {
  it("links to the five sections from the section navigation", async () => {
    renderApp("/bot", { permissions: [...ACCESS, ...OPERATOR] });

    for (const [href, name] of SECTIONS) {
      expect(await screen.findByRole("link", { name })).toHaveAttribute("href", href);
    }
  });

  it.each(SECTIONS)("marks only the open section with aria-current at %s", async (path, openName) => {
    renderApp(path, { permissions: [...ACCESS, ...OPERATOR] });

    expect(await screen.findByRole("link", { name: openName })).toHaveAttribute("aria-current", "page");
    for (const [, name] of SECTIONS) {
      if (name !== openName) expect(screen.getByRole("link", { name })).not.toHaveAttribute("aria-current");
    }
    expect(screen.queryByText("Такой страницы нет")).not.toBeInTheDocument();
  });

  it("goes to another section by its link", async () => {
    const { memory } = renderApp("/bot", { permissions: [...ACCESS, ...OPERATOR] });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("link", { name: "Рассылки" }));

    expect(memory.state.location.pathname).toBe("/bot/broadcasts");
    expect(await screen.findByRole("link", { name: "Рассылки" })).toHaveAttribute("aria-current", "page");
  });

  it("has a heading of its own", async () => {
    renderApp("/bot", { permissions: [...ACCESS, ...OPERATOR] });

    expect(await screen.findByRole("heading", { level: 1, name: "Бот" })).toBeInTheDocument();
  });
});

describe("bot section: routes", () => {
  interface RouteLike {
    path?: string;
    lazy?: unknown;
    children?: RouteLike[];
  }
  function leaves(routes: RouteLike[], base = ""): { full: string; route: RouteLike }[] {
    return routes.flatMap((route) => {
      const full = route.path
        ? route.path.startsWith("/")
          ? route.path
          : `${base}/${route.path}`.replace(/\/+/g, "/")
        : base;
      return route.children?.length ? leaves(route.children, full) : [{ full, route }];
    });
  }

  it("has exactly the five bot routes and every one is a lazy chunk", () => {
    const bot = leaves(router.routes as unknown as RouteLike[]).filter(
      ({ full }) => full === "/bot" || full.startsWith("/bot/"),
    );

    expect(bot.map(({ full }) => full).sort()).toEqual(SECTIONS.map(([path]) => path).sort());
    for (const { full, route } of bot) {
      expect(route.lazy, `${full} must be lazy`).toBeTruthy();
    }
  });
});

describe("bot section: no bot connected", () => {
  it.each(SECTIONS)(
    "shows «Бот не подключён» at %s to a non-superadmin when there are no keys",
    async (path) => {
      renderApp(path, {
        permissions: [...ACCESS, ...VIEWER],
        status: botStatus({ has_keys: false, active_keys: 0 }),
      });

      expect(await screen.findByText("Бот не подключён")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Приглашения" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Создать ключ/ })).not.toBeInTheDocument();
    },
  );

  it("lets a superadmin in without keys, to create the first one", async () => {
    renderApp("/bot", {
      permissions: [...ACCESS, ...SUPERADMIN],
      status: botStatus({ has_keys: false, active_keys: 0 }),
      keys: [],
    });

    expect(await screen.findByRole("button", { name: /Создать ключ/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Приглашения" })).toBeInTheDocument();
    expect(screen.queryByText("Бот не подключён")).not.toBeInTheDocument();
  });

  it("keeps the history reachable when every key is revoked (any key counts as «has a key»)", async () => {
    renderApp("/bot/registrations", {
      permissions: [...ACCESS, ...OPERATOR],
      status: botStatus({ has_keys: true, active_keys: 0 }),
    });

    expect(await screen.findByRole("link", { name: "Регистрации" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByText("Бот не подключён")).not.toBeInTheDocument();
  });
});

describe("bot section: states of the frame", () => {
  it("shows a busy «Загрузка…» until the status arrives", async () => {
    renderApp("/bot", {
      permissions: [...ACCESS, ...OPERATOR],
      respond: { "GET /api/v1/bot-admin/status": () => new Promise<Response>(() => {}) },
    });

    const loading = (await screen.findAllByText("Загрузка…"))[0] as HTMLElement;
    expect(loading.closest("[aria-busy='true']")).not.toBeNull();
    expect(screen.queryByText("Бот не подключён")).not.toBeInTheDocument();
  });

  it("shows an error notice without the backend detail when the status fails", async () => {
    renderApp("/bot", {
      permissions: [...ACCESS, ...VIEWER],
      respond: { "GET /api/v1/bot-admin/status": () => json({ detail: SECRET_DETAIL }, 500) },
    });

    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent(SECRET_DETAIL);
    expect(within(document.body).queryByText(SECRET_DETAIL)).not.toBeInTheDocument();
  });
});

describe("bot section in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("has English navigation without a trace of Russian", async () => {
    renderApp("/bot", { permissions: [...ACCESS, ...OPERATOR] });

    expect(await screen.findByRole("heading", { level: 1, name: "Bot" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Invites" })).toHaveAttribute("href", "/bot/invites");
    expect(screen.getByRole("main").textContent).not.toMatch(/[А-Яа-яЁё]/);
    expect(document.documentElement.lang).toBe("en");
  });

  it("says in English that no bot is connected", async () => {
    renderApp("/bot", {
      permissions: [...ACCESS, ...VIEWER],
      status: botStatus({ has_keys: false, active_keys: 0 }),
    });

    const main = await screen.findByRole("main");
    await screen.findByText(/not connected/i);
    expect(main.textContent).not.toMatch(/[А-Яа-яЁё]/);
  });
});
