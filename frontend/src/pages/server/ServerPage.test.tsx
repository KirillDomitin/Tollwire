import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, renderWithProviders } from "../../test/render";
import { ServerPage } from "./ServerPage";

const ID = "11111111-1111-4111-8111-111111111111";

const baseServer = {
  id: ID,
  name: "lab-1",
  host: "203.0.113.20",
  ssh_port: 22,
  status: "new",
  description: null,
  health_status: null,
  last_health_at: null,
  ssh_host_key_fingerprint: null,
  created_at: "2026-10-01T10:00:00Z",
};

const credential = {
  id: "c1",
  server_id: ID,
  auth_type: "password",
  username: "root",
  created_at: "2026-10-01T10:00:00Z",
};
const protocol = {
  id: "p1",
  server_id: ID,
  type: "amnezia_wg",
  port: 51820,
  created_at: "2026-10-01T10:00:00Z",
};

interface Backend {
  server?: Record<string, unknown>;
  credential?: unknown;
  protocols?: unknown[];
  jobs?: unknown[];
  patchStatus?: number;
}

/** Подменный API: отвечает по методу и пути, запоминает все запросы. */
function mockApi({
  server = {},
  credential: cred = null,
  protocols = [],
  jobs = [],
  patchStatus = 200,
}: Backend = {}) {
  const calls: string[] = [];
  let current: Record<string, unknown> = { ...baseServer, ...server };
  const fetchMock = vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    calls.push(`${request.method} ${path}`);
    const base = `/api/v1/servers/${ID}`;
    if (request.method === "GET" && path === base) return json(current);
    if (request.method === "PATCH" && path === base) {
      const body = (await request.json()) as Record<string, unknown>;
      calls.push(`PATCH body ${JSON.stringify(body)}`);
      if (patchStatus !== 200) return json({ detail: "conflict" }, patchStatus);
      current = { ...current, ...body };
      return json(current);
    }
    if (path === `${base}/credentials`) return cred ? json(cred) : json({ detail: "not set" }, 404);
    if (path === `${base}/protocols`) return json(protocols);
    if (path === "/api/v1/jobs") return json(jobs);
    if (request.method === "DELETE" && path === base) return new Response(null, { status: 204 });
    return json({ detail: "unexpected" }, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

function renderCard(permissions: string[] | "all" = "all") {
  return renderWithProviders(<ServerPage />, {
    route: `/servers/${ID}`,
    path: "/servers/:serverId",
    permissions,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Шаг чек-листа по названию и его отметка («готово», «следующий шаг»…). */
function step(title: string) {
  const item = screen.getByText(title).closest("li")!;
  return { item, mark: item.getAttribute("data-state") };
}

describe("ServerPage setup checklist", () => {
  it("starts with pinning the key and explains why later steps are locked", async () => {
    mockApi();
    renderCard();

    expect(await screen.findByRole("heading", { name: "Подготовка сервера" })).toBeInTheDocument();
    expect(step("Закрепить ключ сервера").mark).toBe("next");
    expect(step("Задать SSH-доступ").mark).toBe("locked");
    expect(within(step("Задать SSH-доступ").item).getByText(/Сначала закрепите ключ/)).toBeInTheDocument();
    expect(step("Установить Docker и Amnezia").mark).toBe("locked");
    expect(step("Установить AmneziaWG").mark).toBe("locked");
  });

  it("offers installation once the key is pinned and access is set", async () => {
    mockApi({ server: { ssh_host_key_fingerprint: "SHA256:abc" }, credential });
    renderCard();

    expect(await screen.findByRole("button", { name: "Установить" })).toBeInTheDocument();
    expect(step("Закрепить ключ сервера").mark).toBe("done");
    expect(step("Задать SSH-доступ").mark).toBe("done");
    expect(step("Установить Docker и Amnezia").mark).toBe("next");
  });

  it("shows a running installation instead of the button", async () => {
    const job = {
      id: "j1",
      type: "install_server",
      status: "running",
      progress: 0,
      server_id: ID,
      created_at: new Date().toISOString(),
      started_at: null,
    };
    mockApi({ server: { ssh_host_key_fingerprint: "SHA256:abc" }, credential, jobs: [job] });
    renderCard();

    expect(await screen.findByText(/Идёт установка/)).toBeInTheDocument();
    expect(step("Установить Docker и Amnezia").mark).toBe("running");
    expect(screen.queryByRole("button", { name: "Установить" })).not.toBeInTheDocument();
  });

  it("treats an installed protocol as proof of installation even if the status lags", async () => {
    mockApi({
      server: { ssh_host_key_fingerprint: "SHA256:abc", status: "new" },
      credential,
      protocols: [protocol],
    });
    renderCard();

    expect(await screen.findByRole("heading", { name: "Обслуживание" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Подготовка сервера" })).not.toBeInTheDocument();
    expect(screen.getByText("порт 51820, UDP")).toBeInTheDocument();
  });
});

describe("ServerPage permissions and dangerous actions", () => {
  it("gives a viewer a read-only card", async () => {
    mockApi({
      server: { ssh_host_key_fingerprint: "SHA256:abc", status: "ready" },
      credential,
      protocols: [protocol],
    });
    renderCard(["servers:read"]);

    expect(await screen.findByRole("heading", { name: "Обслуживание" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Проверить состояние" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Заменить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Опасные действия" })).not.toBeInTheDocument();
  });

  it("renames a server and shows the new name", async () => {
    const calls = mockApi();
    renderCard();

    await userEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    const dialog = await screen.findByRole("dialog");
    const name = within(dialog).getByLabelText("Имя в панели");
    expect(name).toHaveValue("lab-1");
    await userEvent.clear(name);
    await userEvent.type(name, "frankfurt-1");
    await userEvent.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByRole("heading", { level: 1, name: "frankfurt-1" })).toBeInTheDocument();
    expect(calls).toContain('PATCH body {"name":"frankfurt-1","description":null}');
  });

  it("clears the note with null and trims the name", async () => {
    const calls = mockApi({ server: { description: "стойка 4" } });
    renderCard();

    await userEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    const dialog = await screen.findByRole("dialog");
    const note = within(dialog).getByLabelText("Заметка (необязательно)");
    expect(note).toHaveValue("стойка 4");
    await userEvent.clear(note);
    const name = within(dialog).getByLabelText("Имя в панели");
    await userEvent.clear(name);
    await userEvent.type(name, "  lab-2  ");
    await userEvent.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByRole("heading", { level: 1, name: "lab-2" })).toBeInTheDocument();
    expect(calls).toContain('PATCH body {"name":"lab-2","description":null}');
  });

  it("keeps the dialog open on a duplicate name and drops the error after cancel", async () => {
    mockApi({ patchStatus: 409 });
    renderCard();

    await userEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    let dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Сохранить" }));
    expect(await within(dialog).findByText(/Сервер с таким именем уже есть/)).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await userEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByText(/Сервер с таким именем уже есть/)).not.toBeInTheDocument();
  });

  it("hides editing from a viewer", async () => {
    mockApi();
    renderCard(["servers:read"]);

    await screen.findByRole("heading", { level: 1, name: "lab-1" });
    expect(screen.queryByRole("button", { name: "Изменить" })).not.toBeInTheDocument();
  });

  it("deletes a server only after its name is typed, then returns to the list", async () => {
    const calls = mockApi();
    renderCard();

    await userEvent.click(await screen.findByRole("button", { name: "Удалить сервер" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Удалить сервер" });
    expect(confirm).toBeDisabled();

    await userEvent.type(within(dialog).getByRole("textbox"), "lab-");
    expect(confirm).toBeDisabled();
    expect(calls).not.toContain(`DELETE /api/v1/servers/${ID}`);

    await userEvent.type(within(dialog).getByRole("textbox"), "1");
    await userEvent.click(confirm);
    expect(await screen.findByText("другая страница")).toBeInTheDocument();
    expect(calls).toContain(`DELETE /api/v1/servers/${ID}`);
  });
});

describe("ServerPage setup checklist without SSH access", () => {
  it("keeps one next step: the protocol waits for a pinned key even on an installed server", async () => {
    mockApi({ server: { status: "ready" }, credential });
    renderCard();

    expect(await screen.findByRole("button", { name: "Получить и сверить ключ" })).toBeInTheDocument();
    expect(step("Закрепить ключ сервера").mark).toBe("next");
    expect(step("Установить AmneziaWG").mark).toBe("locked");
    expect(screen.queryByRole("button", { name: "Установить протокол" })).not.toBeInTheDocument();
  });
});

describe("ServerPage audit link", () => {
  it("links to the events of this server for those who may read the journal", async () => {
    mockApi();
    renderCard(["servers:read", "audit:read"]);

    const link = await screen.findByRole("link", { name: "События сервера" });
    expect(link).toHaveAttribute("href", `/audit?entity_type=server&entity_id=${ID}`);
  });

  it("does not show the link without the right to read the journal", async () => {
    mockApi();
    renderCard(["servers:read"]);

    await screen.findByRole("heading", { name: "lab-1" });
    expect(screen.queryByRole("link", { name: "События сервера" })).not.toBeInTheDocument();
  });
});
