import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import QRCode from "qrcode";

import { QrSeries } from "./QrSeries";

vi.mock("qrcode", () => ({ default: { toCanvas: vi.fn(async () => {}) } }));

const CODES = ["chunk-1", "chunk-2", "chunk-3"];
const lastDrawn = () => vi.mocked(QRCode.toCanvas).mock.calls.at(-1)?.[1];

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.mocked(QRCode.toCanvas).mockClear();
});

function setReducedMotion(reduced: boolean) {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query: string) =>
      ({
        matches: reduced && query.includes("prefers-reduced-motion"),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }) as unknown as MediaQueryList,
  );
}

describe("QrSeries (shared by the config dialog and the public page)", () => {
  it("draws the first code and says which one it is", () => {
    render(<QrSeries codes={CODES} />);

    expect(lastDrawn()).toBe("chunk-1");
    expect(screen.getByRole("img", { name: "QR-код конфига" })).toBeInTheDocument();
    expect(screen.getByText("Код 1 из 3")).toBeInTheDocument();
  });

  it("announces the number of the code politely to screen readers", () => {
    render(<QrSeries codes={CODES} />);

    expect(screen.getByText("Код 1 из 3").closest("[aria-live]")).toHaveAttribute("aria-live", "polite");
  });

  it("switches to the next code by the button and wraps around", async () => {
    render(<QrSeries codes={["a", "b"]} />);

    const next = screen.getByRole("button", { name: /Следующий код|Дальше/ });
    await userEvent.click(next);
    expect(lastDrawn()).toBe("b");
    expect(screen.getByText("Код 2 из 2")).toBeInTheDocument();
    await userEvent.click(next);
    expect(lastDrawn()).toBe("a");
  });

  it("turns the codes over by itself every second", () => {
    vi.useFakeTimers();
    render(<QrSeries codes={CODES} />);

    act(() => void vi.advanceTimersByTime(1000));
    expect(screen.getByText("Код 2 из 3")).toBeInTheDocument();
    act(() => void vi.advanceTimersByTime(2000));
    expect(screen.getByText("Код 1 из 3")).toBeInTheDocument();
  });

  it("can be paused, and goes on when the same control is pressed again", async () => {
    vi.useFakeTimers();
    // Testing Library ждёт тик через setTimeout и продвигает часы только для jest: сообщаем ему, что часы поддельные.
    vi.stubGlobal("jest", { advanceTimersByTime: vi.advanceTimersByTime });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<QrSeries codes={CODES} />);

    const toggle = screen.getByRole("button", { name: /Пауза/ });
    await user.click(toggle);
    act(() => void vi.advanceTimersByTime(5000));
    expect(screen.getByText("Код 1 из 3")).toBeInTheDocument(); // стоит на месте: движение можно остановить

    await user.click(toggle);
    act(() => void vi.advanceTimersByTime(1000));
    expect(screen.getByText("Код 2 из 3")).toBeInTheDocument();
  });

  it("does not turn over by itself when the system asks for less motion", () => {
    vi.useFakeTimers();
    setReducedMotion(true);
    render(<QrSeries codes={CODES} />);

    act(() => void vi.advanceTimersByTime(5000));

    expect(screen.getByText("Код 1 из 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Следующий код|Дальше/ })).toBeInTheDocument();
  });

  it("leaves no timer behind when it goes away", () => {
    vi.useFakeTimers();
    const { unmount } = render(<QrSeries codes={CODES} />);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });

  it("shows a single code without the counter and the controls", () => {
    render(<QrSeries codes={["only"]} />);

    expect(lastDrawn()).toBe("only");
    expect(screen.queryByText(/Код \d+ из/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
