import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, renderWithProviders } from "../../test/render";
import { UsersPage } from "../UsersPage";
import { UserPage } from "./UserPage";

vi.mock("qrcode", () => ({ default: { toCanvas: vi.fn(async () => {}) } }));

const USER = "22222222-2222-4222-8222-222222222222";
const SUB = "33333333-3333-4333-8333-333333333333";
const GRANT = "44444444-4444-4444-8444-444444444444";
const SERVER = "55555555-5555-4555-8555-555555555555";
const day = (n: number) => new Date(Date.now() + n * 24 * 60 * 60 * 1000).toISOString();

const user = {
  id: USER,
  username: "alice",
  email: "alice@example.com",
  status: "active",
  description: null,
  created_at: day(-40),
};
const subscription = {
  id: SUB,
  user_id: USER,
  status: "active",
  valid_from: day(-10),
  valid_until: day(30),
  revoked_at: null,
  created_at: day(-10),
};
const grant = {
  id: GRANT,
  subscription_id: SUB,
  server_id: SERVER,
  protocol_id: "p1",
  status: "active",
  revoked_at: null,
  created_at: day(-10),
};
const server = {
  id: SERVER,
  name: "US2",
  host: "203.0.113.30",
  ssh_port: 22,
  status: "ready",
  created_at: day(-60),
};
const credential = { id: "cred-1", access_grant_id: GRANT, revoked_at: null, created_at: day(-9) };

/** Подменный API пользователей: отвечает по методу и пути, запоминает запросы. */
function mockApi({ subscriptions = [subscription], credentials = [credential] as unknown[] } = {}) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const path = new URL(request.url).pathname;
      calls.push(`${request.method} ${path}`);
      if (path === "/api/v1/vpn-users") return json([{ ...user, access: subscriptions[0] ?? null }]);
      if (path === `/api/v1/vpn-users/${USER}`) return json(user);
      if (path === `/api/v1/vpn-users/${USER}/subscriptions`) return json(subscriptions);
      if (path === `/api/v1/subscriptions/${SUB}/grants`) return json([grant]);
      if (path === `/api/v1/access/${GRANT}/credentials`) return json(credentials);
      if (path === "/api/v1/servers") return json([server]);
      if (path === "/api/v1/credentials/cred-1/config") {
        return json({ id: "cred-1", access_grant_id: GRANT, config: "[Interface]\nPrivateKey = test-only" });
      }
      return json({ detail: "unexpected" }, 500);
    }),
  );
  return calls;
}

function renderCard(permissions: string[] | "all" = "all") {
  return renderWithProviders(<UserPage />, { route: `/users/${USER}`, path: "/users/:userId", permissions });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UsersPage", () => {
  it("shows each user's access and links to the card", async () => {
    mockApi();
    renderWithProviders(<UsersPage />);

    const link = await screen.findByRole("link", { name: "alice" });
    expect(link).toHaveAttribute("href", `/users/${USER}`);
    const row = link.closest("tr")!;
    expect(row).toHaveAttribute("data-signal", "ok");
    expect(within(row).getByText(/^До /)).toBeInTheDocument();
    expect(screen.getByText("1 пользователь — 1 с доступом")).toBeInTheDocument();
  });
});

describe("UserPage", () => {
  it("lists servers with their config state and offers the config to those who may see it", async () => {
    mockApi();
    renderCard();

    const row = (await screen.findByText(/Конфиг выдан/)).closest("li")!;
    expect(within(row).getByText("US2")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Показать конфиг" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Отозвать конфиг" })).toBeInTheDocument();
  });

  it("hides the config from a viewer and all changes too", async () => {
    mockApi();
    renderCard(["vpn_users:read", "access:read", "servers:read"]);

    expect(await screen.findByText(/Конфиг выдан/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать конфиг" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Изменить срок" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Опасные действия" })).not.toBeInTheDocument();
  });

  it("offers issuing a config when the server is granted but no config is live", async () => {
    mockApi({ credentials: [{ ...credential, revoked_at: day(-1) }] });
    renderCard();

    expect(await screen.findByRole("button", { name: "Выдать конфиг" })).toBeInTheDocument();
    expect(screen.getByText("Конфиг не выдан")).toBeInTheDocument();
  });

  it("offers granting access when the user has none, mentioning how the last one ended", async () => {
    mockApi({ subscriptions: [{ ...subscription, status: "expired", valid_until: day(-2) }] });
    renderCard();

    expect(await screen.findByRole("button", { name: "Выдать доступ" })).toBeInTheDocument();
    expect(screen.getByText(/Сейчас доступа нет — последний истёк/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Прошлые доступы" })).toBeInTheDocument();
  });

  it("reveals the config only on request and forgets it when the dialog closes", async () => {
    const calls = mockApi();
    renderCard();

    await userEvent.click(await screen.findByRole("button", { name: "Показать конфиг" }));
    expect(calls).not.toContain("POST /api/v1/credentials/cred-1/config"); // открыть окно ≠ показать конфиг
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Показать конфиг" }),
    );
    expect(await screen.findByRole("img", { name: "QR-код конфига" })).toBeInTheDocument();
    expect(calls.filter((call) => call === "POST /api/v1/credentials/cred-1/config")).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await userEvent.click(screen.getByRole("button", { name: "Показать конфиг" }));
    // Снова открыли — конфига нет, пока его явно не запросят ещё раз.
    expect(within(screen.getByRole("dialog")).queryByRole("img")).not.toBeInTheDocument();
  });
});
