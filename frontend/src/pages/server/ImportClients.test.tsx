import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import { addDays, localDay } from "../../lib/time";
import { json, renderWithProviders } from "../../test/render";
import { ServerPage } from "./ServerPage";
import { keys } from "./useServer";

const ID = "11111111-1111-4111-8111-111111111111";
const JOB = "77777777-7777-4777-8777-777777777777";

const server = {
  id: ID,
  name: "lab-1",
  host: "203.0.113.20",
  ssh_port: 22,
  status: "ready",
  description: null,
  health_status: "ok",
  last_health_at: "2026-10-01T10:00:00Z",
  ssh_host_key_fingerprint: "SHA256:abc",
  created_at: "2026-10-01T10:00:00Z",
};
const credential = {
  id: "c1",
  server_id: ID,
  auth_type: "password",
  username: "root",
  created_at: "2026-10-01T10:00:00Z",
};

const minutesAgo = (n: number) => new Date(Date.now() - n * 60_000).toISOString();

const client = (overrides: Record<string, unknown>) => ({
  public_key: "KEY",
  address: "10.8.1.2/32",
  name: null,
  latest_handshake: null,
  supported: true,
  status: "new",
  ...overrides,
});

const clients = [
  client({
    public_key: "KEY-NEW-1=",
    address: "10.8.1.2/32",
    name: "Иван iPhone",
    latest_handshake: minutesAgo(5),
  }),
  client({ public_key: "KEY-NEW-2=", address: "10.8.1.3/32", name: null }),
  client({ public_key: "KEY-IN-PANEL=", address: "10.8.1.4/32", name: "Anna", status: "in_panel" }),
  client({
    public_key: "KEY-REVOKED=",
    address: "10.8.1.5/32",
    name: "Old phone",
    status: "revoked_in_panel",
  }),
  client({ public_key: "KEY-ODD=", address: null, name: "Weird", supported: false, status: "unsupported" }),
];

const v1 = {
  type: "amnezia_app_awg",
  container: "amnezia-awg",
  state: "running",
  image: "amneziavpn/amnezia-wg:latest",
  port: 34567,
  clients,
};
const v2 = {
  type: "amnezia_app_awg2",
  container: "amnezia-awg2",
  state: "running",
  image: "amneziavpn/amnezia-wg:latest",
  port: 34568,
  clients: [client({ public_key: "KEY-V2=", address: "10.9.1.2/32", name: "Tablet" })],
};
const appRow = {
  id: "p-app",
  server_id: ID,
  type: "amnezia_app_awg",
  port: 34567,
  created_at: "2026-10-02T10:00:00Z",
};
const ownRow = { ...appRow, id: "p-own", type: "amnezia_wg", port: 51820 };

const job = (overrides: Record<string, unknown> = {}) => ({
  id: JOB,
  type: "import_clients",
  status: "pending",
  progress: 0,
  server_id: ID,
  result: null,
  error: null,
  created_at: new Date().toISOString(),
  started_at: null,
  ...overrides,
});

interface Backend {
  hasCredential?: boolean;
  protocols?: unknown[];
  discovery?: { checked_at: string | null; protocols: unknown[] };
  discoveryStatus?: number;
  jobs?: unknown[];
  importStatus?: number;
  /** Тело ответа на отказ (не 202) — например, 422 с loc полей. */
  importError?: unknown;
  /** Состояния задачи переноса в списке задач сервера по очереди опросов; последнее повторяется. */
  jobStates?: unknown[];
}

/** Подменный API карточки сервера: отвечает по методу и пути, запоминает запросы с телами. */
function mockApi({
  hasCredential = true,
  protocols = [],
  discovery = { checked_at: minutesAgo(3), protocols: [v1] },
  discoveryStatus = 200,
  jobs = [],
  importStatus = 202,
  importError = { detail: "no" },
  jobStates = [job({ status: "success" })],
}: Backend = {}) {
  const calls: string[] = [];
  const bodies: Record<string, unknown> = {};
  let polled = 0;
  let posted = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const path = new URL(request.url).pathname;
      const base = `/api/v1/servers/${ID}`;
      calls.push(`${request.method} ${path}`);
      if (request.method === "GET" && path === base) return json(server);
      if (path === `${base}/credentials`) {
        return hasCredential ? json(credential) : json({ detail: "not set" }, 404);
      }
      if (path === `${base}/protocols`) return json(protocols);
      if (path === `${base}/discovery`) {
        return discoveryStatus === 200 ? json(discovery) : json({ detail: "x" }, discoveryStatus);
      }
      if (path === "/api/v1/jobs") {
        // После запуска переноса его задача лежит в общем списке задач сервера - тем же опросом, что и остальные.
        const mine = posted ? [jobStates[Math.min(polled++, jobStates.length - 1)]] : [];
        return json([...mine, ...jobs]);
      }
      if (request.method === "POST" && path === `${base}/import`) {
        bodies.import = await request.json();
        if (importStatus !== 202) return json(importError, importStatus);
        posted = true;
        return json(job(), 202);
      }
      if (request.method === "POST" && path === `${base}/discover`)
        return json(job({ type: "discover_vpn" }), 202);
      if (path === "/api/v1/vpn-users") return json([]);
      return json({ detail: "unexpected" }, 500);
    }),
  );
  return { calls, bodies };
}

function renderCard(permissions: string[] | "all" = "all", tab = "config") {
  // Блок приложения AmneziaVPN — во вкладке «Конфигурация», журнал задач — в «Задачах».
  return renderWithProviders(<ServerPage />, {
    route: `/servers/${ID}?tab=${tab}`,
    path: "/servers/:serverId",
    permissions,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Строка клиента в диалоге — по адресу. */
const row = (address: string) => screen.getByText(address).closest("li")!;

async function openDialog() {
  await userEvent.click(await screen.findByRole("button", { name: "Подхватить клиентов" }));
  return screen.findByRole("dialog", { name: "Подхватить клиентов AmneziaVPN" });
}

describe("the AmneziaVPN block on the server card", () => {
  it("shows the found installation with the number of clients and how many are new", async () => {
    mockApi();
    renderCard();

    const block = (await screen.findByRole("heading", { name: "На сервере уже есть AmneziaVPN" })).closest(
      "section",
    )!;
    expect(within(block).getByText("amnezia-awg")).toBeInTheDocument();
    expect(within(block).getByText("34567, UDP")).toBeInTheDocument();
    expect(within(block).getByText("работает")).toBeInTheDocument();
    expect(within(block).getByText("всего 5, новых: 2")).toBeInTheDocument();
    expect(within(block).getByRole("button", { name: "Подхватить клиентов" })).toBeInTheDocument();
  });

  it("shows the block to a viewer without the import button", async () => {
    mockApi();
    renderCard(["servers:read"]);

    expect(
      await screen.findByRole("heading", { name: "На сервере уже есть AmneziaVPN" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Подхватить клиентов" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Найти существующую Amnezia" })).not.toBeInTheDocument();
  });

  it("is hidden when nothing was found or the report fails", async () => {
    mockApi({ discovery: { checked_at: minutesAgo(3), protocols: [] }, protocols: [ownRow] });
    const { unmount } = renderCard();
    await screen.findByRole("heading", { name: "Обслуживание" });
    expect(screen.queryByRole("heading", { name: "На сервере уже есть AmneziaVPN" })).not.toBeInTheDocument();
    expect(await screen.findByText(/установленной AmneziaVPN не найдено/)).toBeInTheDocument();
    unmount();

    mockApi({ discoveryStatus: 500, protocols: [ownRow] });
    renderCard();
    await screen.findByRole("heading", { name: "Обслуживание" });
    expect(screen.queryByRole("heading", { name: "На сервере уже есть AmneziaVPN" })).not.toBeInTheDocument();
  });

  it("does not ask for the report while the panel has no SSH access", async () => {
    const { calls } = mockApi({ hasCredential: false });
    renderCard();

    await screen.findByRole("heading", { name: "Подготовка сервера" });
    expect(calls.some((call) => call.endsWith("/discovery"))).toBe(false);
  });

  it("says there is nothing to take over when every client is already in the panel", async () => {
    const taken = { ...v1, clients: clients.filter((item) => item.status === "in_panel") };
    mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [taken] } });
    renderCard();

    expect(
      await screen.findByText(/Подхватывать нечего: новых клиентов нет\. Создайте клиента/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Подхватить клиентов" })).not.toBeInTheDocument();
  });

  it("warns about a stopped container", async () => {
    mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [{ ...v1, state: "stopped" }] } });
    renderCard();

    expect(await screen.findByText(/Контейнер остановлен/)).toBeInTheDocument();
    expect(screen.getByText("остановлен")).toBeInTheDocument();
  });

  it("shows the busy button while a takeover is running", async () => {
    mockApi({ jobs: [job({ status: "running" })] });
    renderCard();

    const button = await screen.findByRole("button", { name: "Идёт перенос клиентов…" });
    expect(button).toBeDisabled();
  });

  it("starts a search from maintenance", async () => {
    const { calls } = mockApi({ protocols: [ownRow], discovery: { checked_at: null, protocols: [] } });
    renderCard();

    await userEvent.click(await screen.findByRole("button", { name: "Найти существующую Amnezia" }));
    await waitFor(() => expect(calls).toContain(`POST /api/v1/servers/${ID}/discover`));
  });

  it("describes a server that has only the app's protocol as ready, without the option to remove AmneziaWG", async () => {
    mockApi({ protocols: [appRow] });
    renderCard();

    expect(await screen.findByRole("heading", { name: "Обслуживание" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Подготовка сервера" })).not.toBeInTheDocument();
    expect(screen.getByText("AmneziaVPN (приложение)")).toBeInTheDocument();
    expect(screen.getByText("порт 34567, UDP")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Снять AmneziaWG" })).not.toBeInTheDocument();
  });
});

describe("installing the panel's own protocol next to the app", () => {
  it("does not offer it when the app's v1 container is found: the name is taken", async () => {
    mockApi();
    renderCard();

    expect(await screen.findByText(/Это имя занято приложением/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Установить протокол" })).not.toBeInTheDocument();
    // Поиск можно повторить и отсюда.
    expect(screen.getByRole("button", { name: "Найти существующую Amnezia" })).toBeInTheDocument();
  });

  it("keeps the button but warns when only AWG 2.0 is found", async () => {
    mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [v2] } });
    renderCard();

    expect(await screen.findByText(/протоколом AWG 2.0/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Установить протокол" })).toBeInTheDocument();
  });

  it("offers it as usual when no app is found", async () => {
    mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [] } });
    renderCard();

    expect(await screen.findByRole("button", { name: "Установить протокол" })).toBeInTheDocument();
    expect(screen.queryByText(/Это имя занято приложением/)).not.toBeInTheDocument();
  });
});

describe("ImportClientsDialog", () => {
  it("selects new clients by default and locks those that cannot be taken over", async () => {
    mockApi();
    renderCard();
    const dialog = await openDialog();

    const box = (address: string) => within(row(address)).getByRole("checkbox");
    expect(box("10.8.1.2/32")).toBeChecked();
    expect(box("10.8.1.3/32")).toBeChecked();
    // Уже в панели и неподдерживаемые недоступны и объяснены.
    expect(box("10.8.1.4/32")).toBeDisabled();
    expect(box("10.8.1.4/32")).not.toBeChecked();
    expect(
      within(row("10.8.1.4/32")).getByText(/уже в панели — повторно подхватывать не нужно/),
    ).toBeInTheDocument();
    const odd = within(dialog).getByText("без адреса").closest("li")!;
    expect(within(odd).getByRole("checkbox")).toBeDisabled();
    expect(within(odd).getByText(/только клиентов с одним адресом IPv4/)).toBeInTheDocument();
    // Отозванный в панели — доступен, но не выбран.
    expect(box("10.8.1.5/32")).toBeEnabled();
    expect(box("10.8.1.5/32")).not.toBeChecked();
    expect(within(dialog).getByText("Выбрано: 2")).toBeInTheDocument();
  });

  it("shows the app name, the last handshake and defaults for the user name", async () => {
    mockApi();
    renderCard();
    await openDialog();

    const first = row("10.8.1.2/32");
    expect(within(first).getByText(/в приложении: Иван iPhone/)).toBeInTheDocument();
    expect(within(first).getByText(/подключался 5 минут назад/)).toBeInTheDocument();
    expect(within(first).getByLabelText("Имя пользователя")).toHaveValue("Иван iPhone");
    const second = row("10.8.1.3/32");
    expect(within(second).getByText(/без имени · ни разу не подключался/)).toBeInTheDocument();
    expect(within(second).getByLabelText("Имя пользователя")).toHaveValue("Client 2");
    expect(within(first).getByLabelText("Email (необязательно)")).toHaveValue("");
  });

  it("warns that configs cannot be shown and that the app should not be used in parallel", async () => {
    mockApi();
    renderCard();
    const dialog = await openDialog();

    const warning = within(dialog).getByText(/Рабочие конфиги клиентов не меняются/);
    expect(warning).toHaveTextContent(/приватный ключ есть только в приложении клиента/);
    expect(warning).toHaveTextContent(/панель отзовёт доступ/);
    expect(warning).toHaveTextContent(/не управляйте ими в приложении AmneziaVPN/);
  });

  it("proposes 30 days ahead and does not allow a date before tomorrow", async () => {
    mockApi();
    renderCard();
    const dialog = await openDialog();

    const date = within(dialog).getByLabelText("Доступ действует до");
    expect(date).toHaveValue(localDay(addDays(new Date(), 30)));
    expect(date).toHaveAttribute("min", localDay(addDays(new Date(), 1)));
  });

  it("selects all new clients on request and can clear the selection", async () => {
    mockApi();
    renderCard();
    const dialog = await openDialog();

    await userEvent.click(within(dialog).getByRole("button", { name: "Снять выбор" }));
    expect(within(dialog).getByText("Выбрано: 0")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Выбрать все новые" }));
    expect(within(dialog).getByText("Выбрано: 2")).toBeInTheDocument();
    // Отозванный в панели «новым» не считается.
    expect(within(row("10.8.1.5/32")).getByRole("checkbox")).not.toBeChecked();
  });

  it("checks the email, the name, the date and the selection before sending anything", async () => {
    const { bodies } = mockApi();
    renderCard();
    const dialog = await openDialog();

    const first = row("10.8.1.2/32");
    await userEvent.type(within(first).getByLabelText("Email (необязательно)"), "not-an-email");
    await userEvent.clear(within(row("10.8.1.3/32")).getByLabelText("Имя пользователя"));
    fireEvent.change(within(dialog).getByLabelText("Доступ действует до"), {
      target: { value: localDay(new Date()) },
    });
    await userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));

    expect(await within(dialog).findByText(/Укажите email полностью/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Имя: от 1 до 100 символов/)).toBeInTheDocument();
    expect(within(dialog).getByText(/не раньше завтрашнего/)).toBeInTheDocument();
    expect(bodies.import).toBeUndefined();

    await userEvent.click(within(dialog).getByRole("button", { name: "Снять выбор" }));
    expect(await within(dialog).findByText("Выберите хотя бы одного клиента.")).toBeInTheDocument();
    expect(bodies.import).toBeUndefined();
  });

  it("does not let two clients share an email", async () => {
    const { bodies } = mockApi();
    renderCard();
    const dialog = await openDialog();

    await userEvent.type(within(row("10.8.1.2/32")).getByLabelText("Email (необязательно)"), "a@example.com");
    await userEvent.type(within(row("10.8.1.3/32")).getByLabelText("Email (необязательно)"), "A@example.com");
    await userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));

    expect(await within(dialog).findByText("Этот email уже указан у другого клиента.")).toBeInTheDocument();
    expect(bodies.import).toBeUndefined();
  });

  it("sends the chosen clients with trimmed names, an email only where it was typed, and the end of the day", async () => {
    const { bodies } = mockApi();
    renderCard();
    const dialog = await openDialog();

    const first = row("10.8.1.2/32");
    await userEvent.clear(within(first).getByLabelText("Имя пользователя"));
    await userEvent.type(within(first).getByLabelText("Имя пользователя"), "  Ivan  ");
    await userEvent.type(within(first).getByLabelText("Email (необязательно)"), " ivan@example.com ");
    // Отозванного в панели добавляем руками.
    await userEvent.click(within(row("10.8.1.5/32")).getByRole("checkbox"));
    const lastDay = addDays(new Date(), 100);
    fireEvent.change(within(dialog).getByLabelText("Доступ действует до"), {
      target: { value: localDay(lastDay) },
    });
    await userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));

    await waitFor(() => expect(bodies.import).toBeDefined());
    expect(bodies.import).toEqual({
      protocol_type: "amnezia_app_awg",
      // Ожидание считается независимо от кода: 23:59:59 выбранного дня по местному времени.
      valid_until: new Date(
        lastDay.getFullYear(),
        lastDay.getMonth(),
        lastDay.getDate(),
        23,
        59,
        59,
      ).toISOString(),
      clients: [
        { public_key: "KEY-NEW-1=", username: "Ivan", email: "ivan@example.com" },
        { public_key: "KEY-NEW-2=", username: "Client 2" },
        { public_key: "KEY-REVOKED=", username: "Old phone" },
      ],
    });
    // Выбранных-но-недоступных (в панели, неподдерживаемых) в запросе нет.
    expect(JSON.stringify(bodies.import)).not.toContain("KEY-IN-PANEL");
    expect(JSON.stringify(bodies.import)).not.toContain("KEY-ODD");
  });

  it("follows the job and shows the outcome, then refreshes the report, protocols and users", async () => {
    const { calls } = mockApi({
      jobStates: [
        job({ status: "running" }),
        job({
          status: "success",
          result: {
            protocol_id: "p-app",
            imported: [{ public_key: "KEY-NEW-1=", user_id: "u1", credential_id: "c1" }],
            already: ["KEY-IN-PANEL="],
            gone: ["KEY-GONE="],
            unsupported: [],
            conflict: [{ public_key: "KEY-NEW-2=", reason: "User with this email already exists" }],
          },
        }),
      ],
    });
    const { queryClient } = renderCard();
    const dialog = await openDialog();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const discoveryCalls = () => calls.filter((call) => call.endsWith("/discovery")).length;
    const before = discoveryCalls();
    await userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));

    expect(await within(dialog).findByText("Переносим клиентов…")).toBeInTheDocument();
    expect(await within(dialog).findByText("Подхвачено: 1", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(within(dialog).getByText("Уже были в панели: 1")).toBeInTheDocument();
    expect(within(dialog).getByText("Пропали с сервера: 1")).toBeInTheDocument();
    expect(within(dialog).getByText("Не перенесены из-за конфликтов: 1")).toBeInTheDocument();
    expect(within(dialog).queryByText(/Не поддерживаются/)).not.toBeInTheDocument();
    // Причина конфликта — по-русски, клиент назван адресом.
    expect(within(dialog).getByText("10.8.1.3/32")).toBeInTheDocument();
    expect(within(dialog).getByText(/Пользователь с таким email уже есть/)).toBeInTheDocument();

    await waitFor(() => expect(discoveryCalls()).toBeGreaterThan(before));
    expect(calls.filter((call) => call.endsWith("/protocols")).length).toBeGreaterThan(1);
    // Список пользователей на экране не открыт, поэтому он не перечитывается сразу, а помечается устаревшим.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["vpn-users"] });

    await userEvent.click(within(dialog).getByRole("button", { name: "Готово" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  }, 15_000);

  it("shows that nothing was taken over, and a failed job with its error", async () => {
    mockApi({
      jobStates: [job({ status: "success", result: { protocol_id: "p", imported: [], conflict: [] } })],
    });
    const { unmount } = renderCard();
    let dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));
    expect(await within(dialog).findByText("Ничего не перенесено.")).toBeInTheDocument();
    unmount();

    mockApi({ jobStates: [job({ status: "failed", error: "SSH connection failed" })] });
    renderCard();
    dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));
    expect(await within(dialog).findByText("Перенос не удался: SSH connection failed")).toBeInTheDocument();
  });

  it.each([
    [404, /Сервер не найден/],
    [409, /Перенос не поставлен/],
    [422, /Проверьте данные: имена до 100 символов/],
  ])("explains a %i answer in plain words and keeps the form", async (status, message) => {
    mockApi({ importStatus: status });
    renderCard();
    const dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));

    expect(await within(dialog).findByText(message)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Подхватить" })).toBeEnabled();
  });

  it("asks which installation to use when the app has both, and starts over for the other one", async () => {
    const { bodies } = mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [v1, v2] } });
    renderCard();
    const dialog = await openDialog();

    expect(within(dialog).getByLabelText("AmneziaWG (приложение)")).toBeChecked();
    await userEvent.click(within(dialog).getByLabelText("AmneziaWG 2.0 (приложение)"));
    expect(screen.queryByText("10.8.1.2/32")).not.toBeInTheDocument();
    expect(within(row("10.9.1.2/32")).getByLabelText("Имя пользователя")).toHaveValue("Tablet");
    await userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));

    await waitFor(() => expect(bodies.import).toBeDefined());
    expect(bodies.import).toMatchObject({ protocol_type: "amnezia_app_awg2" });
  });

  it("forgets the choices when closed and reopened", async () => {
    mockApi();
    renderCard();
    let dialog = await openDialog();

    await userEvent.click(within(dialog).getByRole("button", { name: "Снять выбор" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Отмена" }));
    dialog = await openDialog();
    expect(within(dialog).getByText("Выбрано: 2")).toBeInTheDocument();
  });

  it("speaks English", async () => {
    await i18n.changeLanguage("en");
    mockApi();
    renderCard();
    await userEvent.click(await screen.findByRole("button", { name: "Take over clients" }));
    const dialog = await screen.findByRole("dialog", { name: "Take over AmneziaVPN clients" });

    expect(within(dialog).getByLabelText("Access valid until")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Select all new" })).toBeInTheDocument();
    expect(within(dialog).getByText("Selected: 2")).toBeInTheDocument();
    expect(within(row("10.8.1.4/32")).getByText("already in the panel")).toBeInTheDocument();
  });
});

describe("ImportClientsDialog: errors, limits and edge cases", () => {
  const submit = (dialog: HTMLElement) =>
    userEvent.click(within(dialog).getByRole("button", { name: "Подхватить" }));

  it("summarizes the errors and moves focus to the first invalid field", async () => {
    const { bodies } = mockApi();
    renderCard();
    const dialog = await openDialog();

    const email = within(row("10.8.1.2/32")).getByLabelText("Email (необязательно)");
    await userEvent.type(email, "oops");
    await userEvent.clear(within(row("10.8.1.3/32")).getByLabelText("Имя пользователя"));
    await submit(dialog);

    expect(
      await within(dialog).findByText("Есть ошибки в полях: 2. Исправьте их и отправьте снова."),
    ).toBeInTheDocument();
    expect(email).toHaveAttribute("aria-invalid", "true");
    expect(email).toHaveFocus();
    expect(bodies.import).toBeUndefined();
  });

  it("puts focus on the date when it is the first mistake", async () => {
    mockApi();
    renderCard();
    const dialog = await openDialog();

    const date = within(dialog).getByLabelText("Доступ действует до");
    fireEvent.change(date, { target: { value: "" } });
    await submit(dialog);

    expect(
      await within(dialog).findByText(/не раньше завтрашнего и не позже чем через 10 лет/),
    ).toBeInTheDocument();
    expect(date).toHaveFocus();
  });

  it.each([
    ["a year far beyond reach", "20261-01-01"],
    ["a day that does not exist", "2027-02-31"],
    ["more than 10 years ahead", "2099-01-15"],
    ["today", localDay(new Date())],
  ])("rejects %s as the last day of access without sending anything", async (_name, value) => {
    const { bodies } = mockApi();
    renderCard();
    const dialog = await openDialog();

    fireEvent.change(within(dialog).getByLabelText("Доступ действует до"), { target: { value } });
    await submit(dialog);

    expect(await within(dialog).findByText(/не позже чем через 10 лет/)).toBeInTheDocument();
    expect(bodies.import).toBeUndefined();
  });

  it("limits the date field to ten years ahead", async () => {
    mockApi();
    renderCard();
    const dialog = await openDialog();

    const limit = new Date();
    limit.setFullYear(limit.getFullYear() + 10);
    expect(within(dialog).getByLabelText("Доступ действует до")).toHaveAttribute("max", localDay(limit));
  });

  it("names the field group after the client so equal labels can be told apart", async () => {
    mockApi();
    renderCard();
    const dialog = await openDialog();

    const group = within(dialog).getByRole("group", { name: "10.8.1.2/32" });
    expect(within(group).getByLabelText("Имя пользователя")).toHaveValue("Иван iPhone");
    expect(within(dialog).getByRole("group", { name: "10.8.1.3/32" })).toBeInTheDocument();
  });

  it("shows the standing warning as a plain note, not as an alert", async () => {
    mockApi();
    renderCard();
    const dialog = await openDialog();

    const note = within(dialog).getByText(/Рабочие конфиги клиентов не меняются/);
    expect(note.closest('[role="alert"]')).toBeNull();
  });

  it("counts a name in code points: 100 emoji fit, the 101st is cut from the default", async () => {
    const emoji = "😀";
    const crowded = {
      ...v1,
      clients: [client({ public_key: "KEY-EMOJI=", address: "10.8.2.2/32", name: emoji.repeat(101) })],
    };
    const { bodies } = mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [crowded] } });
    renderCard();
    const dialog = await openDialog();

    const name = within(row("10.8.2.2/32")).getByLabelText("Имя пользователя");
    expect(Array.from((name as HTMLInputElement).value)).toHaveLength(100);
    await submit(dialog);

    await waitFor(() => expect(bodies.import).toBeDefined());
    expect((bodies.import as { clients: { username: string }[] }).clients[0]?.username).toBe(
      emoji.repeat(100),
    );
  });

  it("rejects a name of 101 code points typed by hand", async () => {
    const { bodies } = mockApi();
    renderCard();
    const dialog = await openDialog();

    const name = within(row("10.8.1.2/32")).getByLabelText("Имя пользователя");
    fireEvent.change(name, { target: { value: "😀".repeat(101) } });
    await submit(dialog);

    expect(await within(dialog).findByText(/Имя: от 1 до 100 символов/)).toBeInTheDocument();
    expect(bodies.import).toBeUndefined();
  });

  it("gives a client that shows up after the dialog opened a default name and keeps it unselected", async () => {
    mockApi();
    const { queryClient } = renderCard();
    const dialog = await openDialog();

    const extra = client({ public_key: "KEY-LATE=", address: "10.8.1.9/32", name: null });
    act(() => {
      queryClient.setQueryData(keys.discovery(ID), {
        checked_at: minutesAgo(0),
        protocols: [{ ...v1, clients: [...clients, extra] }],
      });
    });

    const late = await waitFor(() => row("10.8.1.9/32"));
    expect(within(late).getByRole("checkbox")).not.toBeChecked();
    await userEvent.click(within(late).getByRole("checkbox"));
    expect(within(row("10.8.1.9/32")).getByLabelText("Имя пользователя")).toHaveValue("Client 6");
    expect(within(dialog).getByText("Выбрано: 3")).toBeInTheDocument();
  });

  it("selects at most 250 new clients at first and on request, and says so", async () => {
    const many = {
      ...v1,
      clients: Array.from({ length: 300 }, (_, index) =>
        client({
          public_key: `KEY-${index}=`,
          address: `10.7.${Math.floor(index / 200)}.${(index % 200) + 2}/32`,
          name: `c${index}`,
        }),
      ),
    };
    mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [many] } });
    renderCard();
    const dialog = await openDialog();

    expect(within(dialog).getByText("Выбрано: 250")).toBeInTheDocument();
    expect(within(dialog).getByText(/не больше 250 клиентов/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Снять выбор" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Выбрать все новые" }));
    expect(within(dialog).getByText("Выбрано: 250")).toBeInTheDocument();
  }, 30_000);

  it("refuses 251 clients picked by hand", async () => {
    const many = {
      ...v1,
      clients: Array.from({ length: 251 }, (_, index) =>
        client({ public_key: `KEY-${index}=`, address: `10.7.0.${index + 2}/32`, name: `c${index}` }),
      ),
    };
    const { bodies } = mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [many] } });
    renderCard();
    const dialog = await openDialog();

    // 250 выбраны сами; 251-й добавляется вручную.
    await userEvent.click(within(row("10.7.0.252/32")).getByRole("checkbox"));
    expect(within(dialog).getByText("Выбрано: 251")).toBeInTheDocument();
    await submit(dialog);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "За один раз можно подхватить не больше 250 клиентов.",
    );
    expect(bodies.import).toBeUndefined();
  }, 30_000);

  it("marks the row the backend names in a 422 and moves focus there", async () => {
    mockApi({
      importStatus: 422,
      importError: {
        detail: [{ loc: ["body", "clients", 1, "email"], msg: "not a valid email address", type: "x" }],
      },
    });
    renderCard();
    const dialog = await openDialog();

    await submit(dialog);

    // Второй клиент в запросе - 10.8.1.3.
    const email = within(row("10.8.1.3/32")).getByLabelText("Email (необязательно)");
    await waitFor(() => expect(email).toHaveAttribute("aria-invalid", "true"));
    expect(email).toHaveFocus();
    expect(within(row("10.8.1.2/32")).getByLabelText("Email (необязательно)")).not.toHaveAttribute(
      "aria-invalid",
    );
    expect(within(dialog).getByText(/Есть ошибки в полях: 1/)).toBeInTheDocument();
  });

  it("falls back to the general text for a 422 without field locations", async () => {
    mockApi({ importStatus: 422, importError: { detail: "bad" } });
    renderCard();
    const dialog = await openDialog();

    await submit(dialog);

    expect(await within(dialog).findByText(/Проверьте данные: имена до 100 символов/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/Есть ошибки в полях/)).not.toBeInTheDocument();
  });

  it("keeps the takeover going when the dialog is closed during the wait", async () => {
    const { calls } = mockApi({ jobStates: [job({ status: "running" })] });
    renderCard();
    const dialog = await openDialog();
    await submit(dialog);

    expect(await within(dialog).findByText("Переносим клиентов…")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Закрыть" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // Задача видна в журнале карточки, кнопка блока занята, отдельного опроса задачи по id нет.
    expect(await screen.findByRole("button", { name: "Идёт перенос клиентов…" })).toBeDisabled();
    await userEvent.click(screen.getByRole("tab", { name: "Задачи" }));
    expect(screen.getByText("Перенос клиентов AmneziaVPN")).toBeInTheDocument();
    expect(calls.some((call) => call.startsWith("GET /api/v1/jobs/"))).toBe(false);
  });

  it("shows the takeover summary in the jobs log", async () => {
    mockApi({
      jobs: [
        job({
          status: "success",
          result: {
            protocol_id: "p",
            imported: [{}, {}],
            already: ["a"],
            gone: [],
            unsupported: [],
            conflict: [],
          },
        }),
      ],
    });
    renderCard("all", "jobs");

    expect(await screen.findByText("Подхвачено: 2 · Уже были в панели: 1")).toBeInTheDocument();
  });

  it("opens maintenance for a server whose app has nothing to take over", async () => {
    const empty = { ...v1, clients: clients.filter((item) => item.status === "unsupported") };
    mockApi({ discovery: { checked_at: minutesAgo(1), protocols: [empty] } });
    renderCard();

    expect(await screen.findByRole("heading", { name: "Обслуживание" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Проверить состояние" })).toBeInTheDocument();
    expect(
      screen.getByText(/Создайте клиента в приложении AmneziaVPN и повторите поиск/),
    ).toBeInTheDocument();
    // Поиск один - в «Обслуживании»; в шаге протокола его нет.
    expect(screen.getAllByRole("button", { name: "Найти существующую Amnezia" })).toHaveLength(1);
  });
});
