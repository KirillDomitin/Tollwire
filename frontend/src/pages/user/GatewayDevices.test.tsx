import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Schemas, VPNCredential } from "../../api/client";
import { json, renderWithProviders } from "../../test/render";
import { GatewayDevices, useGatewayDevices } from "./GatewayDevices";

vi.mock("./ClientMetrics", () => ({
  ClientMetrics: ({ credentialId }: { credentialId: string }) => <span>metrics:{credentialId}</span>,
}));
vi.mock("./GatewayExitNote", () => ({ GatewayExitNote: () => null }));
vi.mock("./ConfigDialog", () => ({
  ConfigDialog: ({ credentialId }: { credentialId: string }) => (
    <div role="dialog">config:{credentialId}</div>
  ),
}));
const gateway = {
  grant_id: "grant-a",
  server_id: "server-a",
  protocol_id: "protocol-a",
  server_name: "Gateway A",
};
const device = (id: string, state: VPNCredential["state"] = "active"): VPNCredential => ({
  id,
  access_grant_id: gateway.grant_id,
  created_at: "2026-11-13T10:00:00Z",
  revoked_at: null,
  source: "issued",
  external_name: null,
  device_name: id,
  state,
  has_config: state === "active",
  is_gateway: true,
});
const data = (devices: VPNCredential[] = [], extra = {}): Schemas["GatewayDevicesRead"] => ({
  limit: 5,
  occupied: devices.length,
  devices,
  gateways: [gateway],
  ...extra,
});
const onJob = vi.fn();
function renderDevices(value = data(), permissions: string[] | "all" = "all") {
  return renderWithProviders(<GatewayDevices userId="owner" userName="alice" data={value} onJob={onJob} />, {
    permissions,
  });
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it("shows all devices and opens only the selected ready config", async () => {
  renderDevices(data([device("phone"), device("laptop"), device("pending", "provisioning")]));
  expect(screen.getByText("Занято 3 / 5")).toBeInTheDocument();
  const pending = screen.getByText("pending").closest("li")!;
  expect(within(pending).getByText("Выдача не завершена")).toBeInTheDocument();
  expect(within(pending).queryByRole("button", { name: "Показать конфиг" })).not.toBeInTheDocument();
  expect(within(pending).queryByText("metrics:pending")).not.toBeInTheDocument();
  const laptop = screen.getByText("laptop").closest("li")!;
  expect(within(laptop).getByText("metrics:laptop")).toBeInTheDocument();
  await userEvent.click(within(laptop).getByRole("button", { name: "Показать конфиг" }));
  expect(await screen.findByRole("dialog")).toHaveTextContent("config:laptop");
});

it("requires a gateway choice and retries the same request UUID", async () => {
  const requests: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      requests.push(await request.json());
      return json({ detail: "failed" }, 503);
    }),
  );
  renderDevices(
    data([], { gateways: [gateway, { ...gateway, grant_id: "grant-b", server_name: "Gateway B" }] }),
  );
  await userEvent.type(screen.getByLabelText("Название устройства"), "Phone");
  expect(screen.getByRole("button", { name: "Добавить устройство" })).toBeDisabled();
  await userEvent.selectOptions(screen.getByLabelText("Шлюз"), "grant-b");
  await userEvent.click(screen.getByRole("button", { name: "Добавить устройство" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  await waitFor(() => expect(screen.getByRole("button", { name: "Добавить устройство" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "Добавить устройство" }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[0]).toEqual(requests[1]);
  expect(requests[0]).toMatchObject({
    grant_id: "grant-b",
    device_name: "Phone",
    request_id: expect.any(String),
  });
});

it("explains a stale counter rejected by the backend", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json({ reason: "device_limit", detail: "device limit" }, 409)),
  );
  renderDevices();
  await userEvent.type(screen.getByLabelText("Название устройства"), "Phone");
  await userEvent.click(screen.getByRole("button", { name: "Добавить устройство" }));
  expect(await screen.findByText(/Лимит устройств достигнут/)).toBeInTheDocument();
});

it("blocks addition at the global limit and keeps viewer read-only", () => {
  const value = data([device("phone"), device("pending", "reserved")], { occupied: 5 });
  const first = renderDevices(value);
  expect(screen.getByRole("button", { name: "Добавить устройство" })).toBeDisabled();
  first.unmount();
  renderDevices(value, ["access:read"]);
  expect(screen.getByText("phone")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("keeps an expired access device occupied while hiding its config", () => {
  renderDevices(data([device("expired-phone")], { gateways: [] }));
  expect(screen.getByText("Занято 1 / 5")).toBeInTheDocument();
  expect(screen.queryByText("Готово")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Показать конфиг" })).not.toBeInTheDocument();
  expect(screen.queryByText("metrics:expired-phone")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Отозвать устройство" })).toBeInTheDocument();
});

it("revokes only the selected credential", async () => {
  const paths: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      paths.push(new URL(request.url).pathname);
      return json({ id: "job", type: "revoke_device", status: "pending" }, 202);
    }),
  );
  renderDevices(data([device("phone"), device("laptop")]));
  await userEvent.click(
    within(screen.getByText("phone").closest("li")!).getByRole("button", { name: "Отозвать устройство" }),
  );
  await userEvent.click(
    within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Отозвать устройство" }),
  );
  await waitFor(() => expect(paths).toEqual(["/api/v1/credentials/phone/revoke"]));
  expect(onJob).toHaveBeenCalledWith("grant-a", expect.objectContaining({ id: "job" }));
});

it("resumes polling a persisted pending device without a locally tracked job", async () => {
  vi.useFakeTimers();
  let count = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json(data([device("phone", count++ === 0 ? "reserved" : "active")]))),
  );
  function ReloadedCard() {
    const query = useGatewayDevices("owner");
    return query.data ? (
      <GatewayDevices userId="owner" userName="alice" data={query.data} onJob={onJob} />
    ) : null;
  }
  const view = renderWithProviders(<ReloadedCard />);
  try {
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(screen.getByText("Ожидает выдачи")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(2_100));
    expect(screen.getByText("Готово")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Показать конфиг" })).toBeInTheDocument();
  } finally {
    view.unmount();
    vi.useRealTimers();
  }
});
