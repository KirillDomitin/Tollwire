import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import QRCode from "qrcode";

import { json, renderWithProviders } from "../../test/render";
import { ConfigDialog } from "./ConfigDialog";

vi.mock("qrcode", () => ({ default: { toCanvas: vi.fn(async () => {}) } }));

// Регресс этапа 25.3: QrSeries и QrCode вынесены из ConfigDialog в общий компонент, диалог админа работает как прежде.
const REVEALED = {
  id: "cred-1",
  access_grant_id: "grant-1",
  config: "[Interface]\nPrivateKey = test-only-private-key",
  share_link: "vpn://test-share-link",
  share_qr: ["test-chunk-1", "test-chunk-2"],
};

function mockReveal() {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      calls.push(`${request.method} ${new URL(request.url).pathname}`);
      return json(REVEALED);
    }),
  );
  return calls;
}

function open() {
  const onOpenChange = vi.fn();
  renderWithProviders(
    <ConfigDialog credentialId="cred-1" fileName="alice-us2" open onOpenChange={onOpenChange} />,
  );
  return onOpenChange;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(QRCode.toCanvas).mockClear();
});

describe("ConfigDialog after the QR series moved to a shared component", () => {
  it("asks for the config only on the button, then shows the series and the hint", async () => {
    const calls = mockReveal();
    open();

    expect(calls).toEqual([]);
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Показать конфиг" }),
    );

    expect(await screen.findByRole("img", { name: "QR-код конфига" })).toBeInTheDocument();
    expect(calls).toEqual(["POST /api/v1/credentials/cred-1/config"]);
    expect(vi.mocked(QRCode.toCanvas).mock.calls.at(-1)?.[1]).toBe("test-chunk-1");
    expect(screen.getByText("Код 1 из 2")).toBeInTheDocument();
  });

  it("turns the series over by the button", async () => {
    mockReveal();
    open();
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Показать конфиг" }),
    );
    await screen.findByText("Код 1 из 2");

    await userEvent.click(screen.getByRole("button", { name: "Следующий код" }));

    expect(screen.getByText("Код 2 из 2")).toBeInTheDocument();
    expect(vi.mocked(QRCode.toCanvas).mock.calls.at(-1)?.[1]).toBe("test-chunk-2");
  });

  it("offers copying the vpn:// link and downloading the .conf", async () => {
    mockReveal();
    const user = userEvent.setup();
    open();
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Показать конфиг" }),
    );

    await user.click(await screen.findByRole("button", { name: "Скопировать ключ vpn://" }));

    expect(await navigator.clipboard.readText()).toBe("vpn://test-share-link");
    expect(screen.getByRole("button", { name: "Скачать .conf" })).toBeInTheDocument();
  });

  it("forgets the config when it is closed", async () => {
    mockReveal();
    const onOpenChange = open();
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Показать конфиг" }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Готово" }));

    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(screen.queryByRole("img", { name: "QR-код конфига" })).not.toBeInTheDocument();
  });
});
