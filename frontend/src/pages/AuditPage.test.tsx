import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router";

import i18n from "../i18n";
import { actionsOfGroup } from "../lib/audit";
import { json, renderWithProviders } from "../test/render";
import { AuditPage } from "./AuditPage";

const ADMIN_ID = "33333333-3333-4333-8333-333333333333";
const SERVER_ID = "11111111-1111-4111-8111-111111111111";
const LIMIT = 100;

const row = (index: number, overrides: Record<string, unknown> = {}) => ({
  id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
  admin_id: ADMIN_ID,
  admin_email: "boss@example.com",
  action: "server_created",
  entity_type: "server",
  entity_id: SERVER_ID,
  metadata: { name: "lab-1" },
  ip_address: "203.0.113.5",
  created_at: new Date(Date.UTC(2026, 9, 10, 12, 0, 0) - index * 1000).toISOString(),
  ...overrides,
});

interface Backend {
  pages?: unknown[][]; // ответы /audit-logs по порядку запросов; последний повторяется
  status?: number;
  actors?: unknown[];
}

/** Подменный API: запоминает адреса запросов к журналу. */
function mockApi({
  pages = [[]],
  status = 200,
  actors = [{ id: ADMIN_ID, email: "boss@example.com" }],
}: Backend = {}) {
  const logCalls: URL[] = [];
  const fetchMock = vi.fn(async (request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/v1/audit-logs/actors") return json(actors);
    if (url.pathname === "/api/v1/audit-logs") {
      logCalls.push(url);
      if (status !== 200) return json({ detail: "boom" }, status);
      return json(pages[Math.min(logCalls.length - 1, pages.length - 1)]);
    }
    return json([]); // списки серверов и пользователей — для имён в строках
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, logCalls };
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname + location.search}</output>;
}

function renderAudit(route = "/audit", permissions: string[] | "all" = ["audit:read"]) {
  return renderWithProviders(
    <>
      <AuditPage />
      <Where />
    </>,
    { route, permissions },
  );
}

const where = () => screen.getByTestId("where").textContent ?? "";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AuditPage access", () => {
  it("asks for nothing and explains the missing right", () => {
    const { fetchMock } = mockApi();
    renderAudit("/audit", ["servers:read"]);

    expect(screen.getByRole("alert")).toHaveTextContent(/прав/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("AuditPage list", () => {
  it("shows who did what, in words", async () => {
    mockApi({
      pages: [[row(1), row(2, { action: "login", admin_id: null, admin_email: null, entity_type: null })]],
    });
    renderAudit();

    expect(await screen.findByText("Добавлен сервер")).toBeInTheDocument();
    expect(screen.getAllByText(/boss@example.com/).length).toBeGreaterThan(0);
    expect(screen.getByText("Вход в панель")).toBeInTheDocument();
    expect(screen.getByText("Система")).toBeInTheDocument();
  });

  it("labels each event with its group as a coloured pill and keeps the time in its own column", async () => {
    mockApi({
      pages: [
        [
          row(1),
          row(2, { action: "login_failed", admin_id: null, admin_email: null, entity_type: null }),
          row(3, { action: "server_install_failed" }),
        ],
      ],
    });
    renderAudit();

    await screen.findByText("Добавлен сервер");
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByText("Серверы")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("Вход")).toBeInTheDocument();
    expect(rows[1]).toHaveAttribute("data-signal", "warn");
    expect(rows[2]).toHaveAttribute("data-signal", "down");
    expect(screen.getByRole("columnheader", { name: "Тип" })).toBeInTheDocument();
  });

  it("does not print secrets that happen to be in the metadata", async () => {
    mockApi({ pages: [[row(1, { metadata: { name: "lab-1", private_key: "TOPSECRETKEY" } })]] });
    renderAudit();

    await screen.findByText("Добавлен сервер");
    expect(document.body).not.toHaveTextContent("TOPSECRETKEY");
  });

  it("says there are no events yet when nothing is filtered", async () => {
    mockApi({ pages: [[]] });
    renderAudit();

    expect(await screen.findByText("Событий пока нет")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Сбросить" })).not.toBeInTheDocument();
  });

  it("says the filters caught nothing and offers to reset them", async () => {
    const { logCalls } = mockApi({ pages: [[]] });
    renderAudit(`/audit?entity_type=server&entity_id=${SERVER_ID}`);

    expect(await screen.findByText("Под фильтры ничего не попало")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Сбросить" }));

    await waitFor(() => expect(where()).toBe("/audit"));
    await waitFor(() => expect(logCalls.at(-1)!.searchParams.has("entity_id")).toBe(false));
  });

  it("explains a failed load and lets the admin retry", async () => {
    const backend = mockApi({ status: 500 });
    renderAudit();

    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер панели ответил ошибкой");
    backend.fetchMock.mockImplementation(async (request: Request) => {
      const url = new URL(request.url);
      return json(url.pathname === "/api/v1/audit-logs" ? [row(1)] : []);
    });
    await userEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByText("Добавлен сервер")).toBeInTheDocument();
  });
});

describe("AuditPage filters", () => {
  it("takes the server filter from the address (link from the server card)", async () => {
    const { logCalls } = mockApi({ pages: [[row(1)]] });
    renderAudit(`/audit?entity_type=server&entity_id=${SERVER_ID}`);

    await screen.findByText("Добавлен сервер");
    expect(logCalls[0]!.searchParams.get("entity_type")).toBe("server");
    expect(logCalls[0]!.searchParams.get("entity_id")).toBe(SERVER_ID);
    expect(logCalls[0]!.searchParams.get("limit")).toBe(String(LIMIT));
  });

  it("fills the admin filter from the list of actors and applies it to the address and the request", async () => {
    const { logCalls } = mockApi({ pages: [[row(1)]] });
    renderAudit();
    await screen.findByText("Добавлен сервер");

    const admins = screen.getByRole("combobox", { name: "Админ" });
    await within(admins).findByRole("option", { name: "boss@example.com" });
    await userEvent.selectOptions(admins, "boss@example.com");

    await waitFor(() => expect(where()).toContain(`admin_id=${ADMIN_ID}`));
    await waitFor(() => expect(logCalls.at(-1)!.searchParams.get("admin_id")).toBe(ADMIN_ID));
  });

  it("sends a group of actions as repeated action parameters", async () => {
    const { logCalls } = mockApi({ pages: [[row(1)]] });
    renderAudit();
    await screen.findByText("Добавлен сервер");

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Группа" }), "Серверы");

    await waitFor(() => expect(logCalls.at(-1)!.searchParams.getAll("action").length).toBeGreaterThan(1));
    expect([...logCalls.at(-1)!.searchParams.getAll("action")].sort()).toEqual(
      [...actionsOfGroup("servers")].sort(),
    );
    expect(where()).not.toBe("/audit");
  });

  it("treats the end date as the whole day: until is the start of the next day, local time", async () => {
    const { logCalls } = mockApi({ pages: [[row(1)]] });
    renderAudit();
    await screen.findByText("Добавлен сервер");

    fireEvent.change(screen.getByLabelText("По дату"), { target: { value: "2026-10-10" } });

    const expected = new Date(2026, 9, 11, 0, 0, 0, 0).toISOString();
    await waitFor(() => expect(logCalls.at(-1)!.searchParams.get("until")).toBe(expected));
  });

  it("treats the start date as the start of that day, local time", async () => {
    const { logCalls } = mockApi({ pages: [[row(1)]] });
    renderAudit();
    await screen.findByText("Добавлен сервер");

    fireEvent.change(screen.getByLabelText("С даты"), { target: { value: "2026-10-05" } });

    const expected = new Date(2026, 9, 5, 0, 0, 0, 0).toISOString();
    await waitFor(() => expect(logCalls.at(-1)!.searchParams.get("since")).toBe(expected));
  });
});

describe("AuditPage paging", () => {
  const fullPage = Array.from({ length: LIMIT }, (_, index) => row(index + 1));

  it("asks for more from the last row's cursor and appends the rows", async () => {
    const older = row(500, { action: "logout", entity_type: null, entity_id: null });
    const { logCalls } = mockApi({ pages: [fullPage, [older]] });
    renderAudit();

    await userEvent.click(await screen.findByRole("button", { name: "Показать ещё" }));

    await screen.findByText("Выход из панели");
    const last = fullPage.at(-1)!;
    expect(logCalls[1]!.searchParams.get("before_created_at")).toBe(last.created_at);
    expect(logCalls[1]!.searchParams.get("before_id")).toBe(last.id);
    expect(logCalls[1]!.searchParams.has("offset")).toBe(false);
    expect(screen.getAllByText("Добавлен сервер")).toHaveLength(LIMIT); // первая страница на месте
  });

  it("hides the button once a short page arrives", async () => {
    mockApi({ pages: [fullPage, [row(500)]] });
    renderAudit();

    await userEvent.click(await screen.findByRole("button", { name: "Показать ещё" }));
    await waitFor(() => expect(screen.getAllByText("Добавлен сервер")).toHaveLength(LIMIT + 1));
    expect(screen.queryByRole("button", { name: "Показать ещё" })).not.toBeInTheDocument();
  });

  it("has no button when fewer rows than the limit came", async () => {
    mockApi({ pages: [[row(1), row(2)]] });
    renderAudit();

    await screen.findAllByText("Добавлен сервер");
    expect(screen.queryByRole("button", { name: "Показать ещё" })).not.toBeInTheDocument();
  });

  it("starts from the first page again when a filter changes", async () => {
    const { logCalls } = mockApi({ pages: [fullPage, [row(500)], [row(1)]] });
    renderAudit();
    await userEvent.click(await screen.findByRole("button", { name: "Показать ещё" }));
    await waitFor(() => expect(logCalls).toHaveLength(2));

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Группа" }), "Серверы");

    await waitFor(() => expect(logCalls.length).toBeGreaterThan(2));
    expect(logCalls.at(-1)!.searchParams.has("before_id")).toBe(false);
  });
});

describe("AuditPage in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("translates headings, filters, events and keeps the group id in the address", async () => {
    const { logCalls } = mockApi({ pages: [[row(1)]] });
    renderAudit();

    expect(await screen.findByRole("heading", { name: "Audit log" })).toBeInTheDocument();
    expect(await screen.findByText("Server added")).toBeInTheDocument();
    expect(screen.getByText("Events shown: 1")).toBeInTheDocument();
    for (const name of ["Event", "Who", "Object", "When"]) {
      expect(screen.getByRole("columnheader", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("combobox", { name: "Admin" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "All events" })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Group" }), "Servers");

    // В адресе — стабильный идентификатор группы, не подпись: ссылка живёт при смене языка.
    await waitFor(() => expect(where()).toContain("group=servers"));
    await waitFor(() =>
      expect([...logCalls.at(-1)!.searchParams.getAll("action")].sort()).toEqual(
        [...actionsOfGroup("servers")].sort(),
      ),
    );
    expect(screen.getByRole("button", { name: "Reset" })).toBeInTheDocument();
  });
});
