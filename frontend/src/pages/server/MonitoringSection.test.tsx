import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, renderWithProviders } from "../../test/render";
import { freshMetrics } from "../ServersPage";
import { breakGaps, MonitoringSection } from "./MonitoringSection";

// uPlot рисует на canvas, которого в jsdom нет. Подмена запоминает, что ему передали: подписи и ряды графиков.
const charts: { labels: string[]; series: number }[] = [];
vi.mock("uplot", () => ({
  default: class {
    constructor(options: { series: { label?: string }[] }) {
      charts.push({
        labels: options.series.slice(1).map((s) => s.label ?? ""),
        series: options.series.length - 1,
      });
    }
    setSize() {}
    destroy() {}
  },
}));

const SERVER = "66666666-6666-4666-8666-666666666666";
const point = (minutesAgo: number, overrides: Record<string, unknown> = {}) => ({
  ts: new Date(Date.now() - minutesAgo * 60_000).toISOString(),
  cpu_percent: 4.2,
  load1: 0.05,
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
  ...overrides,
});

function mockMetrics(byRange: Record<string, unknown>) {
  const ranges: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const range = url.searchParams.get("range") ?? "";
      ranges.push(range);
      return json(byRange[range] ?? { range, resolution: "minute", points: [] });
    }),
  );
  return ranges;
}

afterEach(() => {
  vi.unstubAllGlobals();
  charts.length = 0;
});

describe("MonitoringSection", () => {
  it("summarises the latest sample that has rates and draws five charts", async () => {
    mockMetrics({
      "24h": {
        range: "24h",
        resolution: "minute",
        // Последний замер — первый после перерыва, без скоростей: сводка берёт предыдущий.
        points: [
          point(2),
          point(1, { cpu_percent: 57.8 }),
          point(0, { cpu_percent: null, net_rx_bytes_per_s: null }),
        ],
      },
    });
    renderWithProviders(<MonitoringSection serverId={SERVER} />);

    const summary = (await screen.findByText("CPU", { selector: "dt" })).closest("dl")!;
    expect(within(summary).getByText("58 %")).toBeInTheDocument();
    expect(within(summary).getByText("559 МБ из 960 МБ")).toBeInTheDocument();
    expect(within(summary).getByText("Сеть ↓ / ↑").nextElementSibling).toHaveTextContent(
      "1 Мбит/с / 100 Кбит/с",
    );
    expect(within(summary).getByText("1 из 2")).toBeInTheDocument();
    expect(charts.map((chart) => chart.labels[0])).toEqual(["Онлайн", "CPU", "Память", "Входящий", "Чтение"]);
  });

  it("switches to hourly data for a week and shows the peak next to the average", async () => {
    const ranges = mockMetrics({
      "24h": { range: "24h", resolution: "minute", points: [point(1)] },
      "7d": {
        range: "7d",
        resolution: "hour",
        points: [point(60, { cpu_percent_max: 90 }), point(0, { cpu_percent_max: 75 })],
      },
    });
    renderWithProviders(<MonitoringSection serverId={SERVER} />);
    await screen.findByText("1 из 2");

    await userEvent.click(screen.getByRole("button", { name: "Неделя" }));
    expect(await screen.findByText(/Средние за час/)).toBeInTheDocument();
    expect(ranges).toContain("7d");
    expect(screen.getByRole("button", { name: "Неделя" })).toHaveAttribute("aria-pressed", "true");
    expect(charts.at(-4)?.labels).toEqual(["CPU, среднее", "CPU, пик"]); // порядок: клиенты, CPU, память, сеть, диск
  });

  it("explains an empty period instead of drawing empty charts", async () => {
    mockMetrics({});
    renderWithProviders(<MonitoringSection serverId={SERVER} />);
    expect(await screen.findByText(/Замеров за этот период нет/)).toBeInTheDocument();
    expect(charts).toHaveLength(0);
  });
});

describe("freshMetrics", () => {
  it("drops samples older than five minutes at the time they were received", () => {
    const now = Date.now();
    const item = (server_id: string, minutesAgo: number) =>
      ({ ...point(minutesAgo), server_id }) as Parameters<typeof freshMetrics>[0][number];
    const fresh = freshMetrics([item("a", 1), item("b", 6)], now);
    expect([...fresh.keys()]).toEqual(["a"]);
  });
});

describe("breakGaps", () => {
  it("inserts an empty point where samples are missing so the line breaks instead of bridging", () => {
    const points = [point(20), point(19), point(2), point(1)]; // 17 минут без замеров
    const result = breakGaps(points as Parameters<typeof breakGaps>[0], 3 * 60_000);
    expect(result).toHaveLength(5);
    expect(result[2]?.cpu_percent).toBeNull();
    expect(result[2]?.clients_online).toBeNull();
    expect(new Date(result[2]!.ts).getTime()).toBe(new Date(points[1]!.ts).getTime() + 1000);
  });
});
