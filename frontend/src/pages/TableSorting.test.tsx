import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useLatestMetrics } from "./latestMetrics";
import { act, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../i18n";
import { sortRows, accessRank, serverRank } from "../lib/tableSort";
import type { Server, VPNUserListItem } from "../api/client";
import { json, renderWithProviders } from "../test/render";
import { UsersPage } from "./UsersPage";
import { ServersPage } from "./ServersPage";
const names = () =>
  within(screen.getByRole("table"))
    .getAllByRole("link")
    .map((l) => l.textContent);
const click = async (name: string) => userEvent.click(screen.getByRole("button", { name }));
const user = (id: string, username: string, until?: string) => ({
  id,
  username,
  status: "active",
  access: until ? { status: "active", valid_from: "2020-01-01", valid_until: until } : null,
});
const server = (id: string, name: string, extra = {}) => ({
  id,
  name,
  host: "host",
  ssh_port: 22,
  status: "ready",
  health_status: "ok",
  ...extra,
});
beforeEach(async () => {
  await i18n.changeLanguage("en");
});
afterEach(() => vi.unstubAllGlobals());
it("users: names, dates, missing values, search and refresh", async () => {
  const data = [user("c", "beta"), user("b", "ALPHA", "2099-02-01"), user("a", "alpha", "2099-01-01")];
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json(data)),
  );
  const { queryClient } = renderWithProviders(<UsersPage />, { permissions: [] });
  await screen.findByRole("link", { name: "beta" });
  expect(names()).toEqual(["alpha", "ALPHA", "beta"]);
  const header = screen.getByRole("columnheader", { name: "User" });
  expect(header).toHaveAttribute("aria-sort", "none");
  await click("User");
  expect(names()).toEqual(["alpha", "ALPHA", "beta"]);
  await click("User");
  expect(names()).toEqual(["beta", "alpha", "ALPHA"]);
  await click("Valid until");
  expect(names()).toEqual(["alpha", "ALPHA", "beta"]);
  await click("Valid until");
  expect(names()).toEqual(["ALPHA", "alpha", "beta"]);
  await userEvent.type(screen.getByRole("searchbox"), "alpha");
  expect(names()).toEqual(["ALPHA", "alpha"]);
  const updated = [user("a", "alpha", "2099-03-01"), data[1], data[0]];
  await act(async () => {
    queryClient.setQueryData(["vpn-users"], updated);
  });
  await waitFor(() => expect(names()).toEqual(["alpha", "ALPHA"]));
  expect(queryClient.getQueryData(["vpn-users"])).toEqual(updated);
  expect(screen.getByRole("columnheader", { name: "Valid until" })).toHaveAttribute(
    "aria-sort",
    "descending",
  );
});
it("servers: original order, numeric clients, stale values, keyboard, field switch and refresh", async () => {
  const data = [server("a", "Zulu"), server("b", "alpha"), server("c", "Missing"), server("d", "Stale")];
  const metrics = [
    { server_id: "a", clients_online: 10, ts: new Date().toISOString() },
    { server_id: "b", clients_online: 2, ts: new Date().toISOString() },
    { server_id: "d", clients_online: 0, ts: "2020-01-01" },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (r: Request) => json(r.url.includes("monitoring/latest") ? metrics : data)),
  );
  const { queryClient } = renderWithProviders(<ServersPage />, { permissions: [] });
  await screen.findByRole("link", { name: "Zulu" });
  expect(names()).toEqual(["Zulu", "alpha", "Missing", "Stale"]);
  await waitFor(() => expect(screen.getByText("2 of 0")).toBeInTheDocument());
  screen.getByRole("button", { name: "Clients" }).focus();
  await userEvent.keyboard("{Enter}");
  expect(names()).toEqual(["alpha", "Zulu", "Missing", "Stale"]);
  await click("Clients");
  expect(names()).toEqual(["Zulu", "alpha", "Missing", "Stale"]);
  await click("Name");
  expect(names()).toEqual(["alpha", "Missing", "Stale", "Zulu"]);
  await click("Name");
  await userEvent.type(screen.getByRole("searchbox"), "host");
  const updated = [...data, server("e", "zzzz")];
  await act(async () => {
    queryClient.setQueryData(["servers"], updated);
  });
  await waitFor(() => expect(names()[0]).toBe("zzzz"));
  expect(queryClient.getQueryData(["servers"])).toEqual(updated);
});
it("explicit status order and null-last ID ties in either direction", () => {
  const now = new Date("2030-01-01");
  const a = user("a", "a", "2030-02-01");
  const states = [
    a,
    user("b", "b", "2030-01-02"),
    { ...a, access: { ...a.access!, status: "pending", valid_from: "2030-01-20" } },
    user("d", "d", "2029-01-01"),
    { ...a, access: { ...a.access!, status: "revoked" } },
    user("f", "f"),
  ];
  expect(states.map((u) => accessRank(u as VPNUserListItem, now))).toEqual([0, 1, 2, 3, 4, null]);
  const servers = [
    server("0", ""),
    server("1", "", { health_status: "degraded" }),
    server("2", "", { health_status: "down" }),
    ...["offline", "error", "installing", "new"].map((status) => server(status, "", { status })),
    server("7", "", { health_status: null }),
  ];
  expect(servers.map((s) => serverRank(s as Server))).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  const rows = Object.freeze([
    { id: "b", v: "ALPHA" },
    { id: "a", v: "alpha" },
    { id: "c", v: "" },
  ]);
  for (const d of ["ascending", "descending"] as const) {
    expect(sortRows(rows, d, (r) => r.v).map((r) => r.id)).toEqual(["a", "b", "c"]);
    expect(
      sortRows(
        [
          { id: "c", v: NaN },
          { id: "a", v: 0 },
          { id: "b", v: null },
        ],
        d,
        (r) => r.v,
      ).map((r) => r.id),
    ).toEqual(["a", "b", "c"]);
  }
});

it("expires cached client counts while refresh fails", async () => {
  vi.useFakeTimers();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const sample = { server_id: "a", clients_online: 10, ts: new Date().toISOString() };
  queryClient.setQueryData(["metrics", "latest"], [sample]);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new Error("offline");
    }),
  );
  const { result, unmount } = renderHook(() => useLatestMetrics(), {
    wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>,
  });
  try {
    expect(result.current.get("a")?.clients_online).toBe(10);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(301_000);
    });
    expect(result.current.has("a")).toBe(false);
  } finally {
    unmount();
    queryClient.clear();
    vi.useRealTimers();
  }
});

it("compares dates by timestamp and names using the selected language", async () => {
  const dates = [
    { id: "a", date: "2030-01-01T01:00:00+03:00" },
    { id: "b", date: "2029-12-31T23:00:00Z" },
  ];
  expect(sortRows(dates, "ascending", (r) => Date.parse(r.date)).map((r) => r.id)).toEqual(["a", "b"]);
  const rows = [
    { id: "b", name: "\u0411\u043e\u0440\u0438\u0441" },
    { id: "a", name: "\u0430\u043d\u043d\u0430" },
  ];
  for (const language of ["ru", "en"]) {
    await i18n.changeLanguage(language);
    expect(sortRows(rows, "ascending", (r) => r.name).map((r) => r.id)).toEqual(["a", "b"]);
  }
});
