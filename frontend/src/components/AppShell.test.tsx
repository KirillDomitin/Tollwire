import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import { AuthContext } from "../auth/AuthContext";
import i18n from "../i18n";
import { botStatus, mockBot, type BotBackend } from "../test/botApi";
import { json } from "../test/render";
import { AppShell } from "./AppShell";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4, решение Р6-A). AppShell показывает пункт «Бот» (ссылка на /bot):
 * - при can("bot:read") каркас запрашивает GET /api/v1/bot-admin/status через useQuery (staleTime 5 минут);
 *   пункт виден, если has_keys = true;
 * - тому, у кого есть bot:keys (суперадмину), пункт виден всегда: иначе первый ключ из панели не создать;
 * - без bot:read пункта нет и запрос статуса не уходит;
 * - ошибка запроса статуса пункт не показывает и каркас не ломает.
 * Каркас теперь использует TanStack Query, поэтому в тестах он отрисован внутри QueryClientProvider.
 */

const STATUS = "/api/v1/bot-admin/status";

function renderShell(permissions: string[], backend: BotBackend = {}) {
  const api = mockBot({ permissions, ...backend });
  const auth = {
    state: {
      status: "signed-in" as const,
      admin: { id: "a1", email: "viewer@example.com", role: "viewer", permissions },
    },
    login: async () => {},
    logout: async () => {},
    can: (permission: string) => permissions.includes(permission),
  };
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth as never}>
        <MemoryRouter>
          <AppShell />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
  return { api, ...rendered };
}

/** Дать запросу статуса вернуться и отрисоваться: проверки «пункта нет» не должны проходить из-за гонки. */
async function settled(api: ReturnType<typeof mockBot>) {
  await waitFor(() => expect(api.count("GET", STATUS)).toBeGreaterThanOrEqual(1));
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AppShell navigation", () => {
  it("links to the audit journal for those who may read it", () => {
    renderShell(["servers:read", "audit:read"]);

    expect(screen.getByRole("link", { name: "Журнал" })).toHaveAttribute("href", "/audit");
  });

  it("hides the audit journal from those who may not read it", () => {
    renderShell(["servers:read"]);

    expect(screen.queryByRole("link", { name: "Журнал" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Серверы" })).toBeInTheDocument();
  });

  it("offers to change the password from the account block", async () => {
    renderShell(["servers:read"]);

    await userEvent.click(screen.getByRole("button", { name: "Сменить пароль" }));

    expect(await screen.findByRole("dialog", { name: "Смена пароля" })).toBeInTheDocument();
    expect(screen.getByLabelText("Текущий пароль")).toBeInTheDocument();
  });
});

describe("AppShell: the «Бот» item", () => {
  it("shows the item to those who may read the bot once it has a key", async () => {
    renderShell(["servers:read", "bot:read"], { status: botStatus({ has_keys: true }) });

    expect(await screen.findByRole("link", { name: "Бот" })).toHaveAttribute("href", "/bot");
  });

  it("hides the item while the bot has no key", async () => {
    const { api } = renderShell(["servers:read", "bot:read"], {
      status: botStatus({ has_keys: false, active_keys: 0 }),
    });
    await settled(api);

    expect(screen.queryByRole("link", { name: "Бот" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Серверы" })).toBeInTheDocument();
  });

  it("shows the item to the holder of bot:keys even when the bot has no key", async () => {
    renderShell(["servers:read", "bot:read", "bot:manage", "bot:keys"], {
      status: botStatus({ has_keys: false, active_keys: 0 }),
    });

    expect(await screen.findByRole("link", { name: "Бот" })).toHaveAttribute("href", "/bot");
  });

  it("hides the item from those who may not read the bot, and does not ask the status", async () => {
    const { api } = renderShell(["servers:read", "audit:read"], { status: botStatus({ has_keys: true }) });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
    });

    expect(screen.queryByRole("link", { name: "Бот" })).not.toBeInTheDocument();
    expect(api.count("GET", STATUS)).toBe(0);
  });

  it("hides the item, and keeps the rest of the menu, when the status cannot be read", async () => {
    const { api } = renderShell(["servers:read", "bot:read"], {
      respond: { [`GET ${STATUS}`]: () => json({ detail: "boom" }, 500) },
    });
    await settled(api);

    expect(screen.queryByRole("link", { name: "Бот" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Серверы" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // меню молча обходится без пункта
  });

  it("is named «Bot» in English", async () => {
    await i18n.changeLanguage("en");
    renderShell(["servers:read", "bot:read"]);

    expect(await screen.findByRole("link", { name: "Bot" })).toHaveAttribute("href", "/bot");
  });
});
