import { screen, within } from "@testing-library/react";

import { setAccessToken } from "../api/session";
import i18n from "../i18n";
import { json, renderWithProviders } from "../test/render";
import { ServersPage } from "./ServersPage";

const server = (overrides: Record<string, unknown>) => ({
  id: crypto.randomUUID(),
  name: "srv",
  host: "203.0.113.10",
  ssh_port: 22,
  status: "ready",
  description: null,
  health_status: "ok",
  last_health_at: null,
  ssh_host_key_fingerprint: null,
  created_at: new Date().toISOString(),
  ...overrides,
});

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

describe("ServersPage in English", () => {
  it("shows English headings, summary and cells", async () => {
    const a = server({ name: "de-1" });
    const b = server({ name: "us-1", status: "offline", health_status: null });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        const path = new URL(request.url).pathname;
        if (path === "/api/v1/monitoring/latest") {
          return json([
            {
              server_id: a.id,
              ts: new Date().toISOString(),
              cpu_percent: 4.5,
              clients_total: 3,
              clients_online: 1,
            },
          ]);
        }
        return json([a, b]);
      }),
    );
    renderWithProviders(<ServersPage />);

    expect(await screen.findByRole("heading", { name: "Servers" })).toBeInTheDocument();
    expect(await screen.findByText("2 servers — 1 unreachable, 1 running")).toBeInTheDocument();
    for (const name of ["Server", "Status", "Clients", "CPU", "Last check", "Host key"]) {
      expect(screen.getByRole("columnheader", { name })).toBeInTheDocument();
    }
    const row = screen.getByRole("link", { name: "de-1" }).closest("tr")!;
    expect(await within(row).findByText("1 of 3")).toBeInTheDocument();
    expect(within(row).getByText("4.5%")).toBeInTheDocument();
    expect(within(row).getByText("not checked yet")).toBeInTheDocument();
    expect(within(row).getByText("not pinned")).toBeInTheDocument();
  });

  it("explains the empty state in English", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json([])),
    );
    renderWithProviders(<ServersPage />);
    expect(await screen.findByText(/No servers yet\. A server you add/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add server" })).toBeInTheDocument();
  });
});
