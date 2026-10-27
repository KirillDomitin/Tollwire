import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { router } from "./App";
import { setAccessToken } from "./api/session";
import { json } from "./test/render";

vi.mock("qrcode", () => ({ default: { toCanvas: vi.fn(async () => {}) } }));

// Этап 25.3: `/c` - публичная страница вне входа. AuthProvider живёт на маршрутах входа и защищённых, а не над всем
// роутером, поэтому приложение здесь собрано без внешнего AuthProvider (как после переноса в main.tsx).
const TOKEN = "Tok".padEnd(43, "x");
const PAGE = {
  name: "Иван Петров",
  valid_until: "2026-11-25T12:00:00Z",
  server_name: "US2",
  config: "[Interface]\nPrivateKey = test-only-private-key",
  share_link: "vpn://test-share-link",
  share_qr: ["test-chunk-1"],
  file_name: "ivan-us2",
};

let paths: string[] = [];

function stubBackend() {
  paths = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : new URL(String(input), window.location.origin).href;
      const path = new URL(url).pathname;
      paths.push(`${(input instanceof Request ? input.method : init?.method) ?? "GET"} ${path}`);
      if (path === "/api/v1/public/config") return json(PAGE);
      return json({ detail: "Unauthorized" }, 401); // /auth/refresh и всё остальное: сессии нет
    }),
  );
}

function renderApp(location: string) {
  const memory = createMemoryRouter(router.routes, { initialEntries: [location] });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={memory} />
    </QueryClientProvider>,
  );
  return memory;
}

beforeEach(() => {
  setAccessToken(null);
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("routes", () => {
  it("opens the public config page at /c without a sign-in and stays there", async () => {
    stubBackend();
    window.history.replaceState(null, "", `/c#${TOKEN}`);
    const memory = renderApp(`/c#${TOKEN}`);

    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();

    expect(memory.state.location.pathname).toBe("/c");
    expect(screen.queryByLabelText("Пароль")).not.toBeInTheDocument(); // не форма входа
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument(); // и не каркас панели
  });

  it("does not try to restore an admin session on the public page", async () => {
    stubBackend();
    window.history.replaceState(null, "", `/c#${TOKEN}`);
    renderApp(`/c#${TOKEN}`);
    await screen.findByText("Иван Петров");

    expect(paths).toEqual(["POST /api/v1/public/config"]); // ни POST /auth/refresh, ни GET /auth/me
  });

  it("sends a signed-out visitor of a protected page to /login", async () => {
    stubBackend();
    const memory = renderApp("/servers");

    await waitFor(() => expect(memory.state.location.pathname).toBe("/login"));
    expect(paths).toContain("POST /api/v1/auth/refresh"); // здесь сессию восстанавливают, как и раньше
  });

  it("shows the sign-in form at /login with the auth provider that now lives on the route", async () => {
    stubBackend();
    renderApp("/login");

    expect(await screen.findByLabelText("Пароль")).toBeInTheDocument();
  });
});
