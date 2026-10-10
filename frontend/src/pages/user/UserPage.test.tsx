import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import QRCode from "qrcode";

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
function mockApi({
  subscriptions = [subscription],
  credentials = [credential] as unknown[],
  grants = [grant] as unknown[],
  patchStatus = 200,
} = {}) {
  const calls: string[] = [];
  let currentUser: Record<string, unknown> = { ...user };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const path = new URL(request.url).pathname;
      calls.push(`${request.method} ${path}`);
      if (request.method === "PATCH" && path === `/api/v1/vpn-users/${USER}`) {
        const body = (await request.json()) as Record<string, unknown>;
        calls.push(`PATCH body ${JSON.stringify(body)}`);
        if (patchStatus !== 200) return json({ detail: "conflict" }, patchStatus);
        currentUser = { ...currentUser, ...body };
        return json(currentUser);
      }
      if (path === "/api/v1/vpn-users") return json([{ ...user, access: subscriptions[0] ?? null }]);
      if (path === `/api/v1/vpn-users/${USER}`) return json(currentUser);
      if (path === `/api/v1/vpn-users/${USER}/subscriptions`) return json(subscriptions);
      if (path === `/api/v1/subscriptions/${SUB}/grants`) return json(grants);
      if (path === `/api/v1/access/${GRANT}/credentials`) return json(credentials);
      if (path === "/api/v1/servers") return json([server]);
      if (path === "/api/v1/credentials/cred-1/config") {
        return json({
          id: "cred-1",
          access_grant_id: GRANT,
          config: "[Interface]\nPrivateKey = test-only",
          share_link: "vpn://test-only-link",
          share_qr: ["test-chunk-1", "test-chunk-2"],
        });
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

  it("edits the user's name, email and note", async () => {
    const calls = mockApi();
    renderCard();

    await userEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Email")).toHaveValue("alice@example.com");
    const name = within(dialog).getByLabelText("Имя");
    await userEvent.clear(name);
    await userEvent.type(name, "  Alice B  ");
    await userEvent.type(within(dialog).getByLabelText("Заметка (необязательно)"), "из бухгалтерии");
    await userEvent.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Alice B" })).toBeInTheDocument();
    expect(calls).toContain(
      'PATCH body {"username":"Alice B","email":"alice@example.com","description":"из бухгалтерии"}',
    );
  });

  it("keeps the dialog open on a taken email and drops the error after cancel", async () => {
    mockApi({ patchStatus: 409 });
    renderCard();

    await userEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    let dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Сохранить" }));
    expect(await within(dialog).findByText("Пользователь с таким email уже есть.")).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await userEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByText("Пользователь с таким email уже есть.")).not.toBeInTheDocument();
  });

  it("hides the config from a viewer and all changes too", async () => {
    mockApi();
    renderCard(["vpn_users:read", "access:read", "servers:read"]);

    expect(await screen.findByText(/Конфиг выдан/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать конфиг" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Изменить срок" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Изменить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Опасные действия" })).not.toBeInTheDocument();
  });

  it("offers issuing a config when the server is granted but no config is live", async () => {
    mockApi({ credentials: [{ ...credential, revoked_at: day(-1) }] });
    renderCard();

    expect(await screen.findByRole("button", { name: "Выдать конфиг" })).toBeInTheDocument();
    expect(screen.getByText("Конфиг не выдан")).toBeInTheDocument();
  });

  it("hides revoked grants, but keeps one whose client is still being removed from the server", async () => {
    mockApi({
      grants: [{ ...grant, status: "revoked", revoked_at: day(-1) }],
      credentials: [credential],
    });
    const { unmount } = renderCard();
    expect(await screen.findByText("Убираем клиента с сервера…")).toBeInTheDocument();
    unmount();

    mockApi({
      grants: [{ ...grant, status: "revoked", revoked_at: day(-1) }],
      credentials: [{ ...credential, revoked_at: day(-1) }],
    });
    renderCard();
    expect(await screen.findByText(/серверов в доступе нет/)).toBeInTheDocument();
    expect(screen.queryByText("Отозван")).not.toBeInTheDocument();
    expect(screen.queryByText("US2")).not.toBeInTheDocument();
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
    // Главный формат — AmneziaVPN: серия QR в формате его сканера (по коду за раз), а не текст .conf.
    expect(vi.mocked(QRCode.toCanvas)).toHaveBeenLastCalledWith(
      expect.anything(),
      "test-chunk-1",
      expect.anything(),
    );
    expect(screen.getByText("Код 1 из 2")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Следующий код" }));
    expect(vi.mocked(QRCode.toCanvas)).toHaveBeenLastCalledWith(
      expect.anything(),
      "test-chunk-2",
      expect.anything(),
    );
    expect(screen.getByRole("button", { name: "Скопировать ключ vpn://" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Скачать .conf" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await userEvent.click(screen.getByRole("button", { name: "Показать конфиг" }));
    // Снова открыли — конфига нет, пока его явно не запросят ещё раз.
    expect(within(screen.getByRole("dialog")).queryByRole("img")).not.toBeInTheDocument();
  });
});
