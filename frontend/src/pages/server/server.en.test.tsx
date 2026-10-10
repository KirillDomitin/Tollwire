import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import { json, renderWithProviders } from "../../test/render";
import { HostKeyDialog } from "./HostKeyDialog";
import { MonitoringSection } from "./MonitoringSection";

vi.mock("uplot", () => ({
  default: class {
    setSize() {}
    destroy() {}
  },
}));

const SERVER = "66666666-6666-4666-8666-666666666666";

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("server pages in English", () => {
  it("renders the host key hint with the command in a code element", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          fingerprint: "SHA256:abc",
          public_key: "ssh-ed25519 AAAA",
          pinned_fingerprint: null,
          matches_pinned: null,
        }),
      ),
    );
    renderWithProviders(<HostKeyDialog serverId={SERVER} open onOpenChange={() => {}} />);

    const dialog = await screen.findByRole("dialog", { name: "Pin the server key" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Get the key" }));

    expect(
      await within(dialog).findByText("Fingerprint of the key the server presented"),
    ).toBeInTheDocument();
    const hint = within(dialog).getByText(/Check it against the hosting console/);
    expect(within(hint).getByText("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub").tagName).toBe("CODE");
    expect(within(dialog).getByRole("button", { name: "Matches — pin it" })).toBeInTheDocument();
  });

  it("labels the monitoring summary, periods and last sample in English", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          range: "24h",
          resolution: "minute",
          points: [
            {
              ts: new Date(Date.now() - 60_000).toISOString(),
              cpu_percent: 4.2,
              mem_used_bytes: 586_000_000,
              mem_total_bytes: 1_007_000_000,
              disk_used_bytes: 4_700_000_000,
              disk_total_bytes: 9_400_000_000,
              net_rx_bytes_per_s: 125_000,
              net_tx_bytes_per_s: 12_500,
              disk_read_bytes_per_s: 0,
              disk_write_bytes_per_s: 4096,
              clients_total: 2,
              clients_online: 1,
            },
          ],
        }),
      ),
    );
    renderWithProviders(<MonitoringSection serverId={SERVER} />);

    expect(await screen.findByRole("heading", { name: "Monitoring" })).toBeInTheDocument();
    const summary = (await screen.findByText("Memory", { selector: "dt" })).closest("dl")!;
    expect(within(summary).getByText("559 MB of 960 MB")).toBeInTheDocument();
    expect(within(summary).getByText("Network ↓ / ↑").nextElementSibling).toHaveTextContent(
      "1 Mbit/s / 100 Kbit/s",
    );
    expect(within(summary).getByText("1 of 2")).toBeInTheDocument();
    for (const name of ["Hour", "Day", "Week", "Month"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    const note = screen.getByText(/Last sample/);
    expect(within(note).getByText(/ago/).tagName).toBe("TIME");
  });
});
