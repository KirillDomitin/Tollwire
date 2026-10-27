import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import QRCode from "qrcode";

import i18n from "../../i18n";
import { json, renderWithProviders } from "../../test/render";
import { PublicConfigPage } from "./PublicConfigPage";

vi.mock("qrcode", () => ({ default: { toCanvas: vi.fn(async () => {}) } }));

// Страница личного конфига (этап 25.3): адрес `/c#<токен>`, токен из фрагмента уходит в теле POST /api/v1/public/config.
const TOKEN = "Tok".padEnd(43, "x");
const PAGE = {
  name: "Иван Петров",
  valid_until: "2026-11-25T12:00:00Z",
  server_name: "US2",
  config: "[Interface]\nPrivateKey = test-only-private-key\nAddress = 10.8.1.2/32",
  share_link: "vpn://test-share-link",
  share_qr: ["test-chunk-1", "test-chunk-2"],
  file_name: "ivan-us2",
};

interface Seen {
  method: string;
  url: string;
  headers: Headers;
  body: string;
  credentials: string;
}

let seen: Seen[] = [];

async function describeCall(input: RequestInfo | URL, init?: RequestInit): Promise<Seen> {
  if (input instanceof Request) {
    return {
      method: input.method,
      url: input.url,
      headers: input.headers,
      body: await input.clone().text(),
      credentials: input.credentials,
    };
  }
  return {
    method: init?.method ?? "GET",
    url: new URL(String(input), window.location.origin).href,
    headers: new Headers(init?.headers),
    body: typeof init?.body === "string" ? init.body : "",
    credentials: init?.credentials ?? "same-origin",
  };
}

/** Подменный fetch: помнит каждый запрос; ответ — `answer` (по умолчанию страница человека). */
function stubFetch(answer: () => Response | Promise<Response> = () => json(PAGE)) {
  seen = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    seen.push(await describeCall(input, init));
    return answer();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Открыть страницу так, как открывает человек: фрагмент в адресе (и в роутере, и в window.location). */
function openPage(hash = `#${TOKEN}`) {
  window.history.replaceState(null, "", `/c${hash}`);
  return renderWithProviders(<PublicConfigPage />, { route: `/c${hash}` });
}

const main = () => screen.getByRole("main");
const INVALID = /недействительна/i;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(QRCode.toCanvas).mockClear();
  window.history.replaceState(null, "", "/");
  document.title = "";
});

describe("public config page: what it sends", () => {
  it("posts the token from the fragment in the body, never in the address", async () => {
    stubFetch();
    openPage();
    await screen.findByText("Иван Петров");

    const requests = seen.filter((request) => request.url.includes("/public/config"));
    expect(requests).toHaveLength(1);
    const request = requests[0] as Seen;
    expect(request.method).toBe("POST");
    expect(new URL(request.url).pathname).toBe("/api/v1/public/config");
    expect(JSON.parse(request.body)).toEqual({ token: TOKEN });
    expect(seen.map((item) => item.url).join(" ")).not.toContain(TOKEN);
  });

  it("sends no credentials: no Authorization header and no cookies", async () => {
    stubFetch();
    openPage();
    await screen.findByText("Иван Петров");

    for (const request of seen) {
      expect(request.headers.get("Authorization")).toBeNull();
      expect(request.credentials).toBe("omit");
    }
  });

  it("does not touch the sign-in API at all: no refresh, no profile", async () => {
    stubFetch();
    openPage();
    await screen.findByText("Иван Петров");

    expect(seen.filter((request) => request.url.includes("/api/v1/auth/"))).toEqual([]);
    expect(seen.every((request) => request.url.includes("/api/v1/public/"))).toBe(true);
  });

  it("asks for nothing without a token in the address and says the link is invalid", async () => {
    const fetchMock = stubFetch();
    openPage("");

    expect(await screen.findByText(INVALID)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("public config page: what the person sees", () => {
  it("is a document with one main landmark, one h1 and a title", async () => {
    stubFetch();
    openPage();
    await screen.findByText("Иван Петров");

    expect(main()).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    await waitFor(() => expect(document.title).not.toBe(""));
    expect(document.title).not.toContain("Иван");
    expect(document.title).not.toContain(TOKEN);
  });

  it("shows the name, until when the access lasts and the server", async () => {
    stubFetch();
    openPage();

    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();
    expect(main()).toHaveTextContent(/25\.11\.2026|25 ноября 2026/);
    expect(main()).toHaveTextContent("US2");
  });

  it("shows the vpn:// link in a read-only field", async () => {
    stubFetch();
    openPage();

    const field = await screen.findByDisplayValue("vpn://test-share-link");
    expect(field).toHaveAttribute("readonly");
  });

  it("copies the vpn:// link", async () => {
    stubFetch();
    const user = userEvent.setup();
    openPage();

    await user.click(await screen.findByRole("button", { name: /Скопировать/ }));

    expect(await navigator.clipboard.readText()).toBe("vpn://test-share-link");
  });

  it("tells to copy by hand when the browser has no clipboard", async () => {
    stubFetch();
    const user = userEvent.setup();
    openPage();
    const field = await screen.findByDisplayValue("vpn://test-share-link");
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));

    await user.click(screen.getByRole("button", { name: /Скопировать/ }));

    expect(await screen.findByText(/скопируйте его вручную/i)).toBeInTheDocument();
    expect(field).toBeInTheDocument();
  });

  it("downloads the .conf under the name from the answer", async () => {
    stubFetch();
    const blobs: Blob[] = [];
    const created = vi.fn((blob: Blob) => {
      blobs.push(blob);
      return "blob:config";
    });
    Object.defineProperty(URL, "createObjectURL", { value: created, configurable: true, writable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true, writable: true });
    const downloads: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloads.push(this.download);
    });
    openPage();

    await userEvent.click(await screen.findByText(/Скачать/));

    expect(downloads).toEqual(["ivan-us2.conf"]);
    expect(await blobs[0]?.text()).toBe(PAGE.config);
    click.mockRestore();
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });

  it("shows the QR series for another device with a pause button and a spoken counter", async () => {
    stubFetch();
    openPage();

    expect(await screen.findByRole("img", { name: "QR-код конфига" })).toBeInTheDocument();
    expect(vi.mocked(QRCode.toCanvas).mock.calls.at(-1)?.[1]).toBe("test-chunk-1");
    expect(screen.getByText("Код 1 из 2").closest("[aria-live]")).not.toBeNull();
    expect(screen.getByRole("button", { name: /Пауза/ })).toBeInTheDocument();
  });

  it("explains how to import the config on Android, iOS, Windows and macOS", async () => {
    stubFetch();
    openPage();
    await screen.findByText("Иван Петров");

    const summaries = Array.from(main().querySelectorAll("details > summary")).map(
      (node) => node.textContent,
    );
    for (const platform of ["Android", "iOS", "Windows", "macOS"]) {
      expect(summaries.some((text) => text?.includes(platform))).toBe(true);
    }
  });

  it("warns not to forward the link and says where to get a new one", async () => {
    stubFetch();
    openPage();
    await screen.findByText("Иван Петров");

    expect(main()).toHaveTextContent(/не пересылайте/i);
    expect(main()).toHaveTextContent("/config");
  });

  it("erases the config from the page when it is hidden (pagehide)", async () => {
    stubFetch();
    openPage();
    await screen.findByDisplayValue("vpn://test-share-link");

    act(() => void window.dispatchEvent(new Event("pagehide")));

    await waitFor(() => expect(screen.queryByDisplayValue("vpn://test-share-link")).not.toBeInTheDocument());
    expect(screen.queryByRole("img", { name: "QR-код конфига" })).not.toBeInTheDocument();
    expect(main()).not.toHaveTextContent("Иван Петров");
  });
});

describe("public config page: when it cannot show the config", () => {
  it("answers every 404 with the same neutral message, whatever the server says", async () => {
    const texts: string[] = [];
    for (const detail of ["Not found", "Subscription is not active", "Access grant is revoked"]) {
      stubFetch(() => json({ detail }, 404));
      const { unmount } = openPage();
      await screen.findByText(INVALID);
      expect(main()).not.toHaveTextContent(detail);
      texts.push(main().textContent ?? "");
      unmount();
    }

    expect(new Set(texts).size).toBe(1);
    expect(texts[0]).toContain("/config");
    expect(screen.queryByText("Иван Петров")).not.toBeInTheDocument();
  });

  it("tells to wait on 429, differently from an invalid link", async () => {
    stubFetch(() => new Response("{}", { status: 429, headers: { "Retry-After": "120" } }));
    openPage();

    expect(await screen.findByText(/слишком много/i)).toBeInTheDocument();
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument();
  });

  it("tells about the connection when the network fails, and does not call the link invalid", async () => {
    stubFetch(() => {
      throw new TypeError("Failed to fetch");
    });
    openPage();

    expect(await screen.findByText(/нет связи|соединени|сеть/i)).toBeInTheDocument();
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument();
  });

  it("shows no config pieces next to an error", async () => {
    stubFetch(() => json({ detail: "Not found" }, 404));
    openPage();
    await screen.findByText(INVALID);

    expect(screen.queryByRole("img", { name: "QR-код конфига" })).not.toBeInTheDocument();
    expect(within(main()).queryByRole("button", { name: /Скопировать/ })).not.toBeInTheDocument();
  });
});

describe("public config page in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("speaks English: no Russian is left on the page apart from the person's own name", async () => {
    stubFetch(() => json({ ...PAGE, name: "Alice" }));
    openPage();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(main().textContent).not.toMatch(/[А-Яа-яЁё]/);
    expect(main()).toHaveTextContent(/Nov(ember)? 25, 2026|25 Nov(ember)? 2026|11\/25\/2026/);
    expect(main()).toHaveTextContent("/config");
    expect(document.documentElement.lang).toBe("en");
  });

  it("says in English that the link is not valid", async () => {
    stubFetch(() => json({ detail: "Not found" }, 404));
    openPage();

    expect(await screen.findByText(/not valid|invalid|no longer/i)).toBeInTheDocument();
    expect(main().textContent).not.toMatch(/[А-Яа-яЁё]/);
  });
});
