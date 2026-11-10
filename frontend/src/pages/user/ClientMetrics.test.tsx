import { onlineManager } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";

import i18n from "../../i18n";
import { json, renderWithProviders } from "../../test/render";
import { ClientMetrics } from "./ClientMetrics";

const props = { serverId: "server-1", protocolId: "protocol-1", credentialId: "credential-1" };
function snapshot({
  stale = false,
  handshake = null as string | null,
  downloaded = 1048576 as number | null,
} = {}) {
  return {
    server_id: props.serverId,
    protocols: [
      {
        protocol_id: props.protocolId,
        sampled_at: new Date(Date.now() - (stale ? 600_000 : 10_000)).toISOString(),
        stale,
        clients: [
          {
            credential_id: props.credentialId,
            latest_handshake: handshake,
            downloaded_bytes: downloaded,
            uploaded_bytes: 0,
          },
        ],
      },
    ],
  };
}

afterEach(() => {
  onlineManager.setOnline(true);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("ages the last sample even when offline pauses background queries", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn(async () => json(snapshot()));
  vi.stubGlobal("fetch", fetch);
  renderWithProviders(<ClientMetrics {...props} />);
  await act(() => vi.advanceTimersByTimeAsync(100));
  expect(screen.getByText("Ещё не подключался")).toBeInTheDocument();
  onlineManager.setOnline(false);
  await act(() => vi.advanceTimersByTimeAsync(360_000));
  expect(screen.getByRole("status")).toHaveTextContent("Данные устарели");
  expect(screen.queryByText("Ещё не подключался")).not.toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("shows client download/upload directions, zero bytes and exact handshake time", async () => {
  const handshake = new Date(Date.now() - 60_000).toISOString();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json(snapshot({ handshake }))),
  );
  renderWithProviders(<ClientMetrics {...props} />);
  await screen.findByText("Скачано");
  expect(screen.getByText("1 МБ")).toBeInTheDocument();
  expect(screen.getByText("0 Б")).toBeInTheDocument();
  const time = screen.getByText("1 минуту назад");
  expect(time).toHaveAttribute("datetime", handshake);
  expect(time).toHaveAttribute("title");
  expect(screen.getByText(/с последнего сброса VPN/)).toBeInTheDocument();
});

it("distinguishes a fresh peer without handshakes from missing transfer data", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json(snapshot({ downloaded: null }))),
  );
  renderWithProviders(<ClientMetrics {...props} />);
  expect(await screen.findByText("Ещё не подключался")).toBeInTheDocument();
  expect(screen.getByText("—")).toBeInTheDocument();
  expect(screen.getByText("0 Б")).toBeInTheDocument();
});

it("marks stale samples and does not claim an inactive peer has never connected", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json(snapshot({ stale: true }))),
  );
  renderWithProviders(<ClientMetrics {...props} />);
  expect(await screen.findByRole("status")).toHaveTextContent("Данные устарели");
  expect(screen.queryByText("Ещё не подключался")).not.toBeInTheDocument();
  expect(screen.getByText("1 МБ")).toBeInTheDocument();
});

it.each(["missing", "error", "wrong-device"])(
  "does not turn %s statistics into zero traffic",
  async (kind) => {
    const body = snapshot();
    if (kind === "missing") body.protocols = [];
    if (kind === "wrong-device") body.protocols[0]!.clients[0]!.credential_id = "other-device";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(kind === "error" ? { detail: "error" } : body, kind === "error" ? 500 : 200)),
    );
    renderWithProviders(<ClientMetrics {...props} />);
    expect(await screen.findByText("Статистика устройства пока недоступна")).toBeInTheDocument();
    expect(screen.queryByText("0 Б")).not.toBeInTheDocument();
    expect(screen.queryByText("Ещё не подключался")).not.toBeInTheDocument();
  },
);

it("shares a single server query between device rows", async () => {
  const fetch = vi.fn(async () => json(snapshot()));
  vi.stubGlobal("fetch", fetch);
  renderWithProviders(
    <>
      <ClientMetrics {...props} />
      <ClientMetrics {...props} credentialId="other" />
    </>,
  );
  expect(await screen.findByText("1 МБ")).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("retains the last sample and marks it stale after a refresh failure", async () => {
  const fetch = vi.fn(async () => json(snapshot()));
  vi.stubGlobal("fetch", fetch);
  const { queryClient } = renderWithProviders(<ClientMetrics {...props} />);
  await screen.findByText("1 МБ");
  fetch.mockImplementation(async () => json({ detail: "unavailable" }, 500));
  await queryClient.invalidateQueries({ queryKey: ["client-metrics", props.serverId] });
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Данные устарели"));
  expect(screen.getByText("1 МБ")).toBeInTheDocument();
});

it("renders the statistics in English", async () => {
  await i18n.changeLanguage("en");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json(snapshot())),
  );
  renderWithProviders(<ClientMetrics {...props} />);
  const region = within(await screen.findByRole("region", { name: "Device activity" }));
  expect(await region.findByText("Downloaded")).toBeInTheDocument();
  expect(region.getByText("Uploaded")).toBeInTheDocument();
  expect(region.getByText("Has not connected yet")).toBeInTheDocument();
});
