import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { setAccessToken } from "../api/session";
import { AuthProvider } from "../auth/AuthContext";
import { json, renderWithProviders } from "../test/render";
import { LoginPage } from "./LoginPage";
import { ServersPage } from "./ServersPage";

const server = (overrides: Record<string, unknown>) => ({
  id: crypto.randomUUID(),
  name: "srv",
  host: "203.0.113.10",
  ssh_port: 22,
  status: "ready",
  description: null,
  health_status: "ok",
  last_health_at: new Date(Date.now() - 120_000).toISOString(),
  ssh_host_key_fingerprint: "SHA256:c0o8hD8nK8Hk8iLlmS1rvjq7XF9GAtHVatSEGxTvb+E",
  created_at: new Date().toISOString(),
  ...overrides,
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

describe("ServersPage", () => {
  it("shows each server with its signal, check time and host key state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json([
          server({ name: "US2", host: "203.0.113.10" }),
          server({ name: "de-1", status: "offline", ssh_port: 2222, ssh_host_key_fingerprint: null }),
        ]),
      ),
    );
    renderWithProviders(<ServersPage />);

    expect(await screen.findByText("2 сервера — 1 недоступен, 1 в работе")).toBeInTheDocument();
    const [, us2, de1] = screen.getAllByRole("row");
    expect(us2).toHaveAttribute("data-signal", "ok");
    expect(within(us2!).getByText("В работе")).toBeInTheDocument();
    expect(within(us2!).getByText("2 минуты назад")).toBeInTheDocument();
    expect(de1).toHaveAttribute("data-signal", "down");
    expect(within(de1!).getByText("203.0.113.10:2222")).toBeInTheDocument(); // нестандартный порт виден
    expect(within(de1!).getByText("не закреплён")).toBeInTheDocument();
    expect(within(us2!).getByRole("link", { name: "US2" })).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/servers\//),
    );
  });

  it("summarises the fleet in tiles: total, online, offline and clients from fresh metrics", async () => {
    const a = server({ name: "de-1" });
    const b = server({ name: "us-1", status: "offline" });
    const c = server({ name: "fi-1", health_status: "degraded" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        if (new URL(request.url).pathname === "/api/v1/monitoring/latest") {
          const now = new Date().toISOString();
          return json([
            { server_id: a.id, ts: now, cpu_percent: 10, clients_total: 5, clients_online: 2 },
            { server_id: c.id, ts: now, cpu_percent: 20, clients_total: 7, clients_online: 1 },
            // Устаревший замер в сумму не входит.
            {
              server_id: b.id,
              ts: "2020-01-01T00:00:00Z",
              cpu_percent: 1,
              clients_total: 100,
              clients_online: 0,
            },
          ]);
        }
        return json([a, b, c]);
      }),
    );
    renderWithProviders(<ServersPage />);

    const tile = async (name: string) => within(await screen.findByRole("group", { name }));
    expect((await tile("Всего серверов")).getByText("3")).toBeInTheDocument();
    expect((await tile("Онлайн")).getByText("2")).toBeInTheDocument(); // в работе и с проблемами
    expect((await tile("Оффлайн")).getByText("1")).toBeInTheDocument();
    await vi.waitFor(async () => expect((await tile("Всего клиентов")).getByText("12")).toBeInTheDocument());
  });

  it("filters the table by name or address and says when nothing matches", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json([
          server({ name: "🇩🇪 Germany-1", host: "185.199.12.10" }),
          server({ name: "Finland-1", host: "93.184.216.33" }),
        ]),
      ),
    );
    renderWithProviders(<ServersPage />);

    expect(await screen.findByText("Показано 2 из 2")).toBeInTheDocument();
    const search = screen.getByRole("searchbox", { name: "Поиск по названию или адресу" });

    await userEvent.type(search, "germ");
    expect(screen.getByRole("link", { name: "Germany-1" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Finland-1" })).not.toBeInTheDocument();
    expect(screen.getByText("Показано 1 из 2")).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, "93.184");
    expect(screen.getByRole("link", { name: "Finland-1" })).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, "zzz");
    expect(screen.getByText("Ничего не найдено. Измените запрос.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("offers adding a server only to those who may manage servers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json([])),
    );
    const { unmount } = renderWithProviders(<ServersPage />);
    expect(await screen.findByRole("button", { name: "Добавить сервер" })).toBeInTheDocument();
    unmount();

    renderWithProviders(<ServersPage />, { permissions: ["servers:read"] });
    expect(await screen.findByText(/Серверов пока нет/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Добавить сервер" })).not.toBeInTheDocument();
  });

  it("puts the chosen country flag in front of the new server's name", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        if (request.method === "POST") {
          const body = await request.json();
          bodies.push(body);
          return json(server({ ...body }), 201);
        }
        return json([]);
      }),
    );
    renderWithProviders(<ServersPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Добавить сервер" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.selectOptions(within(dialog).getByLabelText("Страна (необязательно)"), "🇫🇮");
    await userEvent.type(within(dialog).getByLabelText("Имя в панели"), "helsinki-1");
    await userEvent.type(within(dialog).getByLabelText("Адрес (IP или домен)"), "203.0.113.7");
    await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));

    await vi.waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ name: "🇫🇮 helsinki-1", host: "203.0.113.7" });
  });

  it("keeps the name as typed when no country is chosen", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        if (request.method === "POST") {
          const body = await request.json();
          bodies.push(body);
          return json(server({ ...body }), 201);
        }
        return json([]);
      }),
    );
    renderWithProviders(<ServersPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Добавить сервер" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Имя в панели"), "lab-1");
    await userEvent.type(within(dialog).getByLabelText("Адрес (IP или домен)"), "203.0.113.8");
    await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));

    await vi.waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ name: "lab-1" });
  });

  it("explains a failed load and lets the admin retry", async () => {
    const fetchMock = vi.fn(async () => json({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);
    renderWithProviders(<ServersPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер панели ответил ошибкой");
    fetchMock.mockImplementation(async () => json([]));
    await userEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByText(/Серверов пока нет/)).toBeInTheDocument();
  });
});

describe("LoginPage", () => {
  it("says what went wrong when the password is wrong", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: Request | string) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.endsWith("/auth/refresh")) return json({}, 401); // сессии нет — показываем форму
        return json({ detail: "Invalid credentials" }, 401);
      }),
    );
    renderWithProviders(
      <AuthProvider>
        <LoginPage />
      </AuthProvider>,
    );

    await userEvent.type(await screen.findByLabelText("Логин"), "admin@example.com");
    await userEvent.type(screen.getByLabelText("Пароль"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Неверный email или пароль.");
  });
});
