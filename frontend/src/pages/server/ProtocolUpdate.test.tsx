import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, renderWithProviders } from "../../test/render";
import { ServerPage } from "./ServerPage";

const ID = "11111111-1111-4111-8111-111111111111";
const DATE = "2026-10-01T10:00:00Z";
const BASE = `/api/v1/servers/${ID}`;

function setup(
  types = ["amnezia_wg"],
  permissions: string[] | "all" = "all",
  observations: Record<string, unknown>[] = [],
  checks: string[] = [],
) {
  const updates: { type: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === `${BASE}/protocol/updates` && request.method === "GET") {
      return json({ protocols: observations });
    }
    if (url.pathname === `${BASE}/protocol/updates/check` && request.method === "POST") {
      checks.push(url.pathname);
      return json({
        id: "check-job", type: "check_protocol_updates", server_id: ID, status: "pending", progress: 0,
        created_at: DATE, started_at: null, finished_at: null, result: null, error: null,
      }, 202);
    }
    if (url.pathname === `${BASE}/protocol/update` && request.method === "POST") {
      const body = await request.json() as { type: string };
      updates.push(body);
      return json({
        id: "update-job", type: "update_protocol", server_id: ID, status: "pending", progress: 0,
        created_at: DATE, started_at: null, finished_at: null, result: null, error: null,
      }, 202);
    }
    if (url.pathname === BASE) return json({
      id: ID, name: "update-lab", host: "203.0.113.20", ssh_port: 22, status: "ready",
      description: null, health_status: null, last_health_at: null, ssh_host_key_fingerprint: "SHA256:test", created_at: DATE,
    });
    if (url.pathname === `${BASE}/credentials`) return json({
      id: "c1", server_id: ID, auth_type: "password", username: "root", created_at: DATE,
    });
    if (url.pathname === `${BASE}/protocols`) return json(types.map((type, index) => ({
      id: `p${index}`, server_id: ID, type, port: 51820 + index, created_at: DATE,
    })));
    if (url.pathname === "/api/v1/jobs" || url.pathname === "/api/v1/monitoring/latest") return json([]);
    if (url.pathname === `${BASE}/gateway/exits` || url.pathname === `${BASE}/gateway/assignments`
      || url.pathname === "/api/v1/vpn-users" || url.pathname === "/api/v1/geoip/overrides") return json([]);
    if (url.pathname === "/api/v1/geoip") return json({ active: null, candidates: [] });
    if (url.pathname.endsWith("/discovery")) return json({ protocols: [], checked_at: DATE });
    return json({ detail: "unexpected request" }, 404);
  }));
  renderWithProviders(<ServerPage />, {
    route: `/servers/${ID}?tab=config`, path: "/servers/:serverId", permissions,
  });
  return updates;
}

afterEach(() => vi.unstubAllGlobals());

it("asks about VPN interruption before updating the installed AWG container", async () => {
  const updates = setup();
  await userEvent.click(await screen.findByRole("button", { name: /Обновить контейнер/ }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/соединен|подключен|переподключ|разрыв/i)).toBeInTheDocument();
  expect(updates).toEqual([]);
  await userEvent.click(within(dialog).getByRole("button", { name: /Обновить/ }));
  await waitFor(() => expect(updates).toEqual([{ type: "amnezia_wg" }]));
});

it("updates the gateway separately from ordinary AWG", async () => {
  const updates = setup(["amnezia_gateway"]);
  await userEvent.click(await screen.findByRole("button", { name: /Обновить контейнер/ }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: /Обновить/ }));
  await waitFor(() => expect(updates).toEqual([{ type: "amnezia_gateway" }]));
});

it("does not offer container update without servers:manage", async () => {
  setup(["amnezia_wg"], ["servers:read"]);
  await screen.findByRole("heading", { name: "Обслуживание" });
  expect(screen.queryByRole("button", { name: /Обновить контейнер/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Проверить обновления" })).not.toBeInTheDocument();
});

function observed(protocolId: string, protocolType: string, status: string, stale = false) {
  return {
    protocol_id: protocolId, protocol_type: protocolType, status, stale,
    checked_at: "2026-11-04T00:00:00Z", last_attempt_at: "2026-11-04T00:00:00Z",
    current_image_id: `sha256:${"a".repeat(64)}`, target_image_id: `sha256:${"b".repeat(64)}`,
    target_digest: `amneziavpn/amneziawg-go@sha256:${"c".repeat(64)}`, reason: null,
  };
}

it("shows an available-update badge beside the exact protocol on this server", async () => {
  setup(["amnezia_wg", "amnezia_gateway"], ["servers:read"], [
    observed("p0", "amnezia_wg", "current"),
    observed("p1", "amnezia_gateway", "available"),
  ]);
  const badge = await screen.findByText("Доступно обновление");
  expect(screen.getAllByText("Доступно обновление")).toHaveLength(1);
  const row = badge.closest("dd");
  expect(row).not.toBeNull();
  expect(row).toHaveTextContent("51821");
  expect(row).not.toHaveTextContent("51820");
  expect(screen.queryByRole("button", { name: /Обновить контейнер/ })).not.toBeInTheDocument();
});

it("marks old availability observations as stale", async () => {
  setup(["amnezia_wg"], ["servers:read"], [observed("p0", "amnezia_wg", "available", true)]);
  expect(await screen.findByText(/устарел/i)).toBeInTheDocument();
});

it("lets servers:check check now without permission to replace a container", async () => {
  const checks: string[] = [];
  const updates = setup(["amnezia_wg"], ["servers:read", "servers:check"], [], checks);
  await userEvent.click(await screen.findByRole("button", { name: "Проверить обновления" }));
  await waitFor(() => expect(checks).toEqual([`${BASE}/protocol/updates/check`]));
  expect(updates).toEqual([]);
  expect(screen.queryByRole("button", { name: /Обновить контейнер/ })).not.toBeInTheDocument();
});

it("does not offer an update for containers owned by the Amnezia app", async () => {
  setup(["amnezia_app_awg2"]);
  await screen.findByRole("heading", { name: "Обслуживание" });
  expect(screen.queryByRole("button", { name: /Обновить контейнер/ })).not.toBeInTheDocument();
});
