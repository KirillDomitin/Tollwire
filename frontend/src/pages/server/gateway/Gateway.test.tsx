import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../../i18n";
import {
  AMS,
  CRED_ALICE,
  CRED_BOB,
  CRED_CAROL,
  CRED_UNKNOWN,
  EXIT1,
  EXIT2,
  FRA,
  GW,
  LIST_CANDIDATE,
  OPERATOR,
  VIEWER,
  assignmentAlice,
  assignmentBob,
  assignmentUnknown,
  candidateList,
  exit1,
  exit2,
  mockGateway,
  overrideDirect,
  type Backend,
} from "../../../test/gatewayApi";
import { renderWithProviders } from "../../../test/render";
import { ServerPage } from "../ServerPage";

const base = `/api/v1/servers/${GW}`;

/** tab — вкладка карточки: шлюз лежит в «Конфигурации» (config); «Настройки» (settings) — снятие протоколов. */
function setup(options: Backend = {}, tab = "config") {
  const api = mockGateway(options);
  vi.stubGlobal("fetch", vi.fn(api.handler));
  renderWithProviders(<ServerPage />, {
    route: `/servers/${GW}?tab=${tab}`,
    path: "/servers/:serverId",
    permissions: options.permissions ?? "all",
  });
  return api;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const section = async () =>
  (await screen.findByRole("heading", { name: "Шлюз", level: 2 })).closest("section")!;
const rowOf = async (name: string) => (await screen.findByRole("link", { name })).closest("tr")!;
const buttonNames = (container: HTMLElement) =>
  within(container)
    .queryAllByRole("button")
    .map((button) => button.getAttribute("aria-label") ?? button.textContent);

describe("gateway section", () => {
  it("shows the summary and the exits of a server with the gateway protocol", async () => {
    setup({ exits: [exit1, exit2] });
    const gateway = await section();

    expect(await within(gateway).findByText("10.77.0.0/22")).toBeInTheDocument();
    expect(within(gateway).getByText("порт 51821, UDP")).toBeInTheDocument();
    expect(await within(gateway).findByText("исправных 2 из 2")).toBeInTheDocument();
    expect(within(gateway).getByText("2 клиента")).toBeInTheDocument();

    const first = await rowOf("Frankfurt");
    expect(first).toHaveAttribute("data-tone", "ok");
    expect(within(first).getByText("выход 1")).toBeInTheDocument();
    expect(within(first).getByText("Активен")).toBeInTheDocument();
    expect(within(first).getByText("в порядке")).toBeInTheDocument();
    expect(within(first).getByText("2 минуты назад")).toBeInTheDocument();
    expect(within(first).getByText("без предела")).toBeInTheDocument();
    const second = await rowOf("Amsterdam");
    expect(within(second).getByText("10")).toBeInTheDocument(); // лимит клиентов
    expect(within(first).getByRole("link", { name: "Frankfurt" })).toHaveAttribute("href", `/servers/${FRA}`);
  });

  it("is hidden on a server without the gateway protocol and asks nothing about it", async () => {
    const api = setup({ protocols: [{ id: "p1", server_id: GW, type: "amnezia_wg", port: 51820 }] });
    expect(await screen.findByRole("heading", { name: "Сервер: gw-1" })).toBeInTheDocument();
    await screen.findByText("порт 51820, UDP");

    expect(screen.queryByRole("heading", { name: "Шлюз", level: 2 })).not.toBeInTheDocument();
    expect(api.calls.filter((call) => call.includes("/gateway/") || call.includes("/geoip"))).toEqual([]);
  });

  it("warns that clients have no internet when there are no exits or none is healthy", async () => {
    setup({ exits: [] });
    expect(
      await screen.findByText(/Выходов нет, поэтому у клиентов шлюза не будет интернета/),
    ).toBeInTheDocument();
  });

  it("warns when every exit has failed", async () => {
    setup({ exits: [{ ...exit1, health: "down" }] });
    expect(await screen.findByText(/Нет исправных выходов/)).toBeInTheDocument();
    expect(within(await rowOf("Frankfurt")).getByText("Ошибка")).toBeInTheDocument();
  });

  it("marks an exit whose add task is gone as not connected and offers a retry with the same settings", async () => {
    const api = setup({
      exits: [{ ...exit1, state: "peer_created", health: null, last_handshake_at: null }],
    });
    const row = await rowOf("Frankfurt");
    expect(within(row).getByText("Не подключился")).toBeInTheDocument();

    await userEvent.click(within(row).getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(api.bodies[`POST ${base}/gateway/exits`]).toHaveLength(1));
    expect(api.bodies[`POST ${base}/gateway/exits`]?.[0]).toEqual({
      exit_server_id: FRA,
      weight: 3,
      max_clients: null,
    });
  });

  it("shows connecting while the add task runs", async () => {
    const running = {
      id: "j1",
      type: "gateway_add_exit",
      status: "running",
      progress: 0,
      server_id: GW,
      created_at: new Date().toISOString(),
    };
    setup({ exits: [{ ...exit1, state: "pending", health: null }], jobs: [running] });
    expect(within(await rowOf("Frankfurt")).getByText("Подключается")).toBeInTheDocument();
  });
});

describe("gateway section permissions", () => {
  it("gives a viewer the data but no buttons", async () => {
    setup({
      permissions: VIEWER,
      exits: [exit1, exit2],
      assignments: [assignmentAlice],
      candidates: [candidateList],
      overrides: [overrideDirect],
    });
    const gateway = await section();
    await within(gateway).findByRole("link", { name: "alice" });
    await within(gateway).findByText("5.45.200.0/24");
    await within(gateway).findByText("Новый список ждёт подтверждения.");

    expect(buttonNames(gateway)).toEqual([]);
  });

  it("gives an operator moving, pinning and checking, but not exits or the list", async () => {
    setup({
      permissions: OPERATOR,
      exits: [exit1, exit2],
      assignments: [assignmentAlice],
      overrides: [overrideDirect],
    });
    const gateway = await section();
    await within(gateway).findByRole("link", { name: "alice" });

    const names = buttonNames(gateway);
    for (const expected of [
      "Проверить сейчас",
      "Перенести клиента alice",
      "Закрепить клиента alice за текущим выходом",
      "Назначить выход клиенту carol", // у carol нет выхода
    ]) {
      expect(names).toContain(expected);
    }
    for (const forbidden of [
      "Добавить выход",
      "Изменить выход Frankfurt",
      "Снять выход Frankfurt",
      "Обновить из RIPE",
      "Загрузить файл",
      "Применить на шлюзе",
      "Добавить исключение",
    ]) {
      expect(names).not.toContain(forbidden);
    }
  });

  it("gives a superadmin everything", async () => {
    setup({
      exits: [exit1],
      assignments: [assignmentAlice],
      overrides: [overrideDirect],
      candidates: [candidateList],
    });
    const gateway = await section();
    await within(gateway).findByRole("link", { name: "alice" });
    await within(gateway).findByText("5.45.200.0/24");

    const names = buttonNames(gateway);
    for (const expected of [
      "Добавить выход",
      "Изменить выход Frankfurt",
      "Снять выход Frankfurt",
      "Проверить сейчас",
      "Перенести клиента alice",
      "Обновить из RIPE",
      "Загрузить файл",
      "Применить на шлюзе",
      "Активировать список",
      "Добавить исключение",
      "Удалить исключение 5.45.200.0/24",
    ]) {
      expect(names).toContain(expected);
    }
  });
});

describe("adding an exit", () => {
  it("offers only panel servers with AmneziaWG that are not the gateway and not yet exits", async () => {
    setup();
    await userEvent.click(await screen.findByRole("button", { name: "Добавить выход" }));
    const dialog = await screen.findByRole("dialog", { name: "Добавить выход" });

    expect(await within(dialog).findByRole("option", { name: "Amsterdam" })).toBeInTheDocument();
    const options = within(dialog).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual(["Amsterdam"]); // не Frankfurt (уже выход), AppOnly, gw-2, gw-1
  });

  it("says so when there is nobody to add", async () => {
    setup({ exits: [exit1, exit2] });
    await userEvent.click(await screen.findByRole("button", { name: "Добавить выход" }));
    const dialog = await screen.findByRole("dialog", { name: "Добавить выход" });
    expect(await within(dialog).findByText(/Подходящих серверов нет/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Добавить" })).toBeEnabled();
  });

  it("checks the weight and the limit before asking the server", async () => {
    const api = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Добавить выход" }));
    const dialog = await screen.findByRole("dialog", { name: "Добавить выход" });
    await within(dialog).findByRole("option", { name: "Amsterdam" });

    const weight = within(dialog).getByLabelText(/^Вес/);
    await userEvent.clear(weight);
    await userEvent.type(weight, "0");
    await userEvent.type(within(dialog).getByLabelText(/^Предел клиентов/), "abc");
    await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));

    expect(await within(dialog).findByText("Вес — целое число от 1 до 100.")).toBeInTheDocument();
    expect(within(dialog).getByText(/Предел — целое число от 1 до 100000/)).toBeInTheDocument();
    expect(weight).toBeInvalid();
    expect(api.calls.some((call) => call.startsWith("POST"))).toBe(false);

    await userEvent.clear(weight);
    await userEvent.type(weight, "101");
    await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));
    expect(await within(dialog).findByText("Вес — целое число от 1 до 100.")).toBeInTheDocument();
    expect(api.calls.some((call) => call.startsWith("POST"))).toBe(false);
  });

  it("sends the chosen server, the weight and the limit, then closes", async () => {
    const api = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Добавить выход" }));
    const dialog = await screen.findByRole("dialog", { name: "Добавить выход" });
    await within(dialog).findByRole("option", { name: "Amsterdam" });

    const weight = within(dialog).getByLabelText(/^Вес/);
    await userEvent.clear(weight);
    await userEvent.type(weight, "5");
    await userEvent.type(within(dialog).getByLabelText(/^Предел клиентов/), "10");
    await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));

    await waitFor(() => expect(api.bodies[`POST ${base}/gateway/exits`]).toHaveLength(1));
    expect(api.bodies[`POST ${base}/gateway/exits`]?.[0]).toEqual({
      exit_server_id: AMS,
      weight: 5,
      max_clients: 10,
    });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Добавить выход" })).not.toBeInTheDocument(),
    );
    // Задача сразу в журнале сервера (вкладка «Задачи»).
    await userEvent.click(screen.getByRole("tab", { name: "Задачи" }));
    expect(await screen.findByText("Добавление выхода шлюза")).toBeInTheDocument();
  });

  it("sends no limit when the field is empty", async () => {
    const api = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Добавить выход" }));
    const dialog = await screen.findByRole("dialog", { name: "Добавить выход" });
    await within(dialog).findByRole("option", { name: "Amsterdam" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));

    await waitFor(() => expect(api.bodies[`POST ${base}/gateway/exits`]).toHaveLength(1));
    expect(api.bodies[`POST ${base}/gateway/exits`]?.[0]).toEqual({
      exit_server_id: AMS,
      weight: 1,
      max_clients: null,
    });
  });

  it("explains a 409 in plain words and keeps the dialog open", async () => {
    setup({ fail: { [`POST ${base}/gateway/exits`]: { status: 409, detail: "No free exit slots" } } });
    await userEvent.click(await screen.findByRole("button", { name: "Добавить выход" }));
    const dialog = await screen.findByRole("dialog", { name: "Добавить выход" });
    await within(dialog).findByRole("option", { name: "Amsterdam" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));

    expect(
      await within(dialog).findByText(/Выход не добавлен: у шлюза или выхода не задан SSH-доступ/),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(/No free exit slots/)).not.toBeInTheDocument();
  });
});

describe("changing an exit", () => {
  it("sends the new weight and removes the limit when the field is cleared", async () => {
    const api = setup({ exits: [exit2] });
    await userEvent.click(await screen.findByRole("button", { name: "Изменить выход Amsterdam" }));
    const dialog = await screen.findByRole("dialog", { name: "Выход «Amsterdam»" });

    const weight = within(dialog).getByLabelText(/^Вес/);
    const limit = within(dialog).getByLabelText(/^Предел клиентов/);
    expect(weight).toHaveValue("1");
    expect(limit).toHaveValue("10");
    await userEvent.clear(weight);
    await userEvent.type(weight, "7");
    await userEvent.clear(limit);
    await userEvent.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.bodies[`PATCH ${base}/gateway/exits/${EXIT2}`]).toHaveLength(1));
    expect(api.bodies[`PATCH ${base}/gateway/exits/${EXIT2}`]?.[0]).toEqual({ weight: 7, max_clients: null });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(within(await rowOf("Amsterdam")).getByText("7")).toBeInTheDocument();
  });

  it("rejects a bad weight without a request", async () => {
    const api = setup({ exits: [exit2] });
    await userEvent.click(await screen.findByRole("button", { name: "Изменить выход Amsterdam" }));
    const dialog = await screen.findByRole("dialog", { name: "Выход «Amsterdam»" });
    const weight = within(dialog).getByLabelText(/^Вес/);
    await userEvent.clear(weight);
    await userEvent.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await within(dialog).findByText("Вес — целое число от 1 до 100.")).toBeInTheDocument();
    expect(api.calls.some((call) => call.startsWith("PATCH"))).toBe(false);
  });
});

describe("removing an exit", () => {
  const detail =
    "2 client(s) of this exit would be left without an exit and without internet; add another exit first or confirm with ?force=true";

  async function openRemove() {
    await userEvent.click(await screen.findByRole("button", { name: "Снять выход Frankfurt" }));
    return screen.findByRole("alertdialog", { name: "Снять выход «Frankfurt»?" });
  }

  it("removes without force when everyone can move", async () => {
    const api = setup();
    const dialog = await openRemove();
    expect(within(dialog).getByText(/Сейчас через этот выход идёт клиентов: 2/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Снять выход" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.calls.filter((call) => call.startsWith("DELETE"))).toEqual([
      `DELETE ${base}/gateway/exits/${EXIT1}`,
    ]);
    await userEvent.click(screen.getByRole("tab", { name: "Задачи" }));
    expect(await screen.findByText("Снятие выхода шлюза")).toBeInTheDocument();
  });

  it("on 409 shows how many clients would be left and removes anyway only after a second confirmation", async () => {
    const api = setup({ removeStatus: 409, removeDetail: detail });
    const dialog = await openRemove();
    await userEvent.click(within(dialog).getByRole("button", { name: "Снять выход" }));

    expect(
      await within(dialog).findByText(/После переноса 2 клиента останутся без выхода и без интернета/),
    ).toBeInTheDocument();
    expect(api.calls.filter((call) => call.startsWith("DELETE"))).toEqual([
      `DELETE ${base}/gateway/exits/${EXIT1}`,
    ]);
    expect(within(dialog).queryByRole("button", { name: "Снять выход" })).not.toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Снять всё равно" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.calls.filter((call) => call.startsWith("DELETE"))).toEqual([
      `DELETE ${base}/gateway/exits/${EXIT1}`,
      `DELETE ${base}/gateway/exits/${EXIT1}?force=true`,
    ]);
  });

  it("shows a plain message for any other 409 and offers no force", async () => {
    setup({ removeStatus: 409, removeDetail: "A task is already running for this exit" });
    const dialog = await openRemove();
    await userEvent.click(within(dialog).getByRole("button", { name: "Снять выход" }));

    expect(await within(dialog).findByText(/по нему уже идёт другая задача/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Снять всё равно" })).not.toBeInTheDocument();
  });

  it("forgets the confirmation when the dialog is closed", async () => {
    setup({ removeStatus: 409, removeDetail: detail });
    let dialog = await openRemove();
    await userEvent.click(within(dialog).getByRole("button", { name: "Снять выход" }));
    await within(dialog).findByRole("button", { name: "Снять всё равно" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Отмена" }));

    dialog = await openRemove();
    expect(within(dialog).getByRole("button", { name: "Снять выход" })).toBeInTheDocument();
    expect(within(dialog).queryByText(/останутся без выхода/)).not.toBeInTheDocument();
  });

  it("offers to repeat a removal that did not finish", async () => {
    setup({ exits: [{ ...exit1, state: "draining" }] });
    const row = await rowOf("Frankfurt");
    expect(within(row).getByText("Не снят")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Снять выход Frankfurt" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Изменить выход/ })).not.toBeInTheDocument();
  });
});

describe("client assignments", () => {
  const withClients = {
    exits: [exit1, exit2],
    assignments: [assignmentAlice, assignmentBob, assignmentUnknown],
  };

  it("names the clients, shows their exit, reason and pinning, without addresses or keys", async () => {
    setup(withClients);
    const alice = await rowOf("alice");
    expect(within(alice).getByRole("link", { name: "alice" })).toHaveAttribute("href", "/users/u-alice");
    expect(within(alice).getByText("Выход 1 — Frankfurt")).toBeInTheDocument();
    expect(within(alice).getByText("выдан конфиг")).toBeInTheDocument();

    const bob = await rowOf("bob");
    expect(within(bob).getByText("возврат на закреплённый выход")).toBeInTheDocument();
    expect(within(bob).getByText("закреплён за выходом 1 — Frankfurt")).toBeInTheDocument();

    // Клиент, чьё имя не нашлось (нет в списке пользователей), называется коротким номером.
    expect(await screen.findByText(`Клиент ${CRED_UNKNOWN.slice(0, 8)}`)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/10\.77\.0\.(?!0\/22)|PrivateKey|vpn:\/\//);
  });

  it("lists clients without an exit first and warns that they have no internet", async () => {
    setup(withClients);
    const carol = await rowOf("carol");
    expect(carol).toHaveAttribute("data-tone", "down");
    expect(within(carol).getByText("нет выхода")).toBeInTheDocument();
    expect(await screen.findByText(/Без выхода и без интернета: 1 клиент/)).toBeInTheDocument();
    expect(within(await section()).getByText("без выхода: 1")).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1);
    expect(
      within(rows.find((row) => row.textContent?.includes("carol"))!).getByText("нет выхода"),
    ).toBeInTheDocument();
  });

  it("moves a client to another exit and pins it there", async () => {
    const api = setup(withClients);
    await userEvent.click(await screen.findByRole("button", { name: "Перенести клиента alice" }));
    const dialog = await screen.findByRole("dialog", { name: "Перенести клиента alice" });

    // Текущий выход не предлагается.
    expect(
      within(dialog)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Выход 2 — Amsterdam"]);
    await userEvent.click(within(dialog).getByRole("checkbox", { name: "Закрепить за этим выходом" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Перенести" }));

    await waitFor(() =>
      expect(api.bodies[`PATCH ${base}/gateway/assignments/${CRED_ALICE}`]).toEqual([
        { exit_id: EXIT2, pin: true },
      ]),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const alice = await rowOf("alice");
    await waitFor(() => expect(within(alice).getByText("Выход 2 — Amsterdam")).toBeInTheDocument());
    expect(within(alice).getByText("вручную")).toBeInTheDocument();
  });

  it("only moves, without pinning, when the box is not ticked", async () => {
    const api = setup(withClients);
    await userEvent.click(await screen.findByRole("button", { name: "Перенести клиента alice" }));
    const dialog = await screen.findByRole("dialog", { name: "Перенести клиента alice" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Перенести" }));
    await waitFor(() =>
      expect(api.bodies[`PATCH ${base}/gateway/assignments/${CRED_ALICE}`]).toEqual([
        { exit_id: EXIT2, pin: false },
      ]),
    );
  });

  it("pins to the current exit and unpins with one click", async () => {
    const api = setup(withClients);
    await userEvent.click(
      await screen.findByRole("button", { name: "Закрепить клиента alice за текущим выходом" }),
    );
    await waitFor(() =>
      expect(api.bodies[`PATCH ${base}/gateway/assignments/${CRED_ALICE}`]).toEqual([
        { exit_id: null, pin: true },
      ]),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Открепить клиента bob" }));
    await waitFor(() =>
      expect(api.bodies[`PATCH ${base}/gateway/assignments/${CRED_BOB}`]).toEqual([
        { exit_id: null, pin: false },
      ]),
    );
  });

  it("assigns an exit to a client that has none", async () => {
    const api = setup(withClients);
    await userEvent.click(await screen.findByRole("button", { name: "Назначить выход клиенту carol" }));
    const dialog = await screen.findByRole("dialog", { name: "Назначить выход клиенту carol" });
    expect(within(dialog).getAllByRole("option")).toHaveLength(2);
    await userEvent.click(within(dialog).getByRole("button", { name: "Назначить" }));
    await waitFor(() =>
      expect(api.bodies[`PATCH ${base}/gateway/assignments/${CRED_CAROL}`]).toEqual([
        { exit_id: EXIT1, pin: false },
      ]),
    );
  });

  it("explains a 502 (the gateway could not apply the map) and a 409", async () => {
    setup({
      ...withClients,
      fail: {
        [`PATCH ${base}/gateway/assignments/${CRED_ALICE}`]: { status: 502, detail: "Gateway apply failed" },
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Перенести клиента alice" }));
    const dialog = await screen.findByRole("dialog", { name: "Перенести клиента alice" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Перенести" }));
    expect(await within(dialog).findByText(/применить его на шлюзе не удалось/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/Gateway apply failed/)).not.toBeInTheDocument();
  });

  it("explains a 409 on a quick pin next to the row", async () => {
    setup({
      ...withClients,
      fail: { [`PATCH ${base}/gateway/assignments/${CRED_ALICE}`]: { status: 409, detail: "Exit failed" } },
    });
    await userEvent.click(
      await screen.findByRole("button", { name: "Закрепить клиента alice за текущим выходом" }),
    );
    expect(
      await screen.findByText(/выход не активен или отказал, клиент отозван либо шлюз занят/),
    ).toBeInTheDocument();
  });

  it("offers no moving to a viewer, and 'check now' to a user who may check", async () => {
    const api = setup({ ...withClients, permissions: [...VIEWER, "servers:check"] });
    await rowOf("alice");
    expect(screen.queryByRole("button", { name: /Перенести клиента/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Закрепить клиента/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Проверить сейчас" }));
    await waitFor(() => expect(api.calls).toContain(`POST ${base}/gateway/check`));
    // Задача идёт - кнопка занята, повторно не нажать.
    expect(await screen.findByRole("button", { name: "Проверяем…" })).toBeDisabled();
  });

  it("shows only a page of rows and loads more on request", async () => {
    const many = Array.from({ length: 30 }, (_, index) => ({
      ...assignmentAlice,
      credential_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    }));
    setup({ exits: [exit1], assignments: many });
    // Три клиента из доступов (alice, bob, carol) в назначениях не встречаются - они «без выхода» и идут первыми.
    const more = await screen.findByRole("button", { name: "Показать ещё (показано 25 из 33)" });
    const table = more.parentElement!.previousElementSibling as HTMLElement;
    expect(within(table).getAllByRole("row")).toHaveLength(26); // шапка + 25
    await userEvent.click(more);
    expect(within(table).getAllByRole("row")).toHaveLength(34);
    expect(screen.queryByRole("button", { name: /Показать ещё/ })).not.toBeInTheDocument();
  });
});

describe("Russia direct", () => {
  it("shows the active list and applies it on the gateway", async () => {
    const api = setup();
    const gateway = await section();
    expect(await within(gateway).findByText("20261019")).toBeInTheDocument();
    expect(within(gateway).getByText(/8\D612/)).toBeInTheDocument();

    await userEvent.click(within(gateway).getByRole("button", { name: "Применить на шлюзе" }));
    await waitFor(() => expect(api.calls).toContain(`POST ${base}/gateway/ru/apply`));
    expect(await within(gateway).findByRole("button", { name: "Применяем…" })).toBeDisabled();
  });

  it("says that no list is loaded and cannot be applied", async () => {
    setup({ active: null });
    const gateway = await section();
    expect(await within(gateway).findByText(/Список ещё не загружен/)).toBeInTheDocument();
    expect(within(gateway).getByRole("button", { name: "Применить на шлюзе" })).toBeDisabled();
  });

  it("explains a refused apply", async () => {
    setup({ fail: { [`POST ${base}/gateway/ru/apply`]: { status: 409, detail: "No active geoip list" } } });
    await userEvent.click(await screen.findByRole("button", { name: "Применить на шлюзе" }));
    expect(await screen.findByText(/Применить нельзя: нет активного списка/)).toBeInTheDocument();
  });

  it("shows the result of the last apply from the task log", async () => {
    setup({
      jobs: [
        {
          id: "j9",
          type: "gateway_apply_ru",
          status: "success",
          progress: 100,
          server_id: GW,
          created_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
          error: null,
          result: { routes: 8612, table: 200, prefixes: 8612 },
        },
      ],
    });
    expect(await screen.findByText(/Применено на шлюзе, маршрутов: 8612/)).toBeInTheDocument();
  });

  it("updates the list from RIPE and reports what came out", async () => {
    const api = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Обновить из RIPE" }));
    await waitFor(() => expect(api.calls).toContain("POST /api/v1/geoip/update"));
    expect(
      await screen.findByText(/Список принят и активен: версия 20261026, диапазонов 8700/),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(api.calls.filter((call) => call === "GET /api/v1/geoip").length).toBeGreaterThan(1),
    );
  });

  it("tells that a downloaded list waits for confirmation", async () => {
    setup({ updateResult: { status: "candidate", prefixes: 4100, version: "20261026" } });
    await userEvent.click(await screen.findByRole("button", { name: "Обновить из RIPE" }));
    expect(
      await screen.findByText(/заметно отличается от активного: подтвердите его ниже/),
    ).toBeInTheDocument();
  });

  it("confirms a candidate list", async () => {
    const api = setup({ candidates: [candidateList] });
    expect(await screen.findByText("Новый список ждёт подтверждения.")).toBeInTheDocument();
    expect(screen.getByText(/Версия 20261026, диапазонов 4\D100/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Активировать список" }));
    await waitFor(() => expect(api.calls).toContain(`POST /api/v1/geoip/${LIST_CANDIDATE}/activate`));
    await waitFor(() =>
      expect(screen.queryByText("Новый список ждёт подтверждения.")).not.toBeInTheDocument(),
    );
  });

  it("explains a candidate that is no longer one", async () => {
    setup({
      candidates: [candidateList],
      fail: {
        [`POST /api/v1/geoip/${LIST_CANDIDATE}/activate`]: {
          status: 409,
          detail: "Only a candidate list can be activated",
        },
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Активировать список" }));
    expect(await screen.findByText(/Подтвердить можно только список-кандидат/)).toBeInTheDocument();
  });

  describe("uploading a file", () => {
    const open = async () => {
      await userEvent.click(await screen.findByRole("button", { name: "Загрузить файл" }));
      return screen.findByRole("dialog", { name: "Загрузить список из файла" });
    };
    const file = (text = "5.45.0.0/16\n") => new File([text], "ru.txt", { type: "text/plain" });

    it("sends the file as plain text and reports the result", async () => {
      const api = setup();
      const dialog = await open();
      await userEvent.upload(within(dialog).getByLabelText("Файл со списком"), file());
      await userEvent.click(within(dialog).getByRole("button", { name: "Загрузить" }));

      await waitFor(() => expect(api.bodies["POST /api/v1/geoip/upload"]).toEqual(["5.45.0.0/16\n"]));
      expect(
        await screen.findByText(/Список принят и активен: версия 20261030, диапазонов 8800/),
      ).toBeInTheDocument();
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    });

    it("needs a file", async () => {
      const api = setup();
      const dialog = await open();
      await userEvent.click(within(dialog).getByRole("button", { name: "Загрузить" }));
      expect(await within(dialog).findByText("Выберите файл.")).toBeInTheDocument();
      expect(api.calls.some((call) => call.includes("upload"))).toBe(false);
    });

    it("refuses a file over 5 MB without sending it", async () => {
      const api = setup();
      const dialog = await open();
      const big = file();
      Object.defineProperty(big, "size", { value: 6 * 1024 * 1024 });
      await userEvent.upload(within(dialog).getByLabelText("Файл со списком"), big);
      expect(await within(dialog).findByText("Файл больше 5 МБ.")).toBeInTheDocument();
      await userEvent.click(within(dialog).getByRole("button", { name: "Загрузить" }));
      expect(api.calls.some((call) => call.includes("upload"))).toBe(false);
    });

    it("explains a 422 in plain words", async () => {
      setup({
        fail: {
          "POST /api/v1/geoip/upload": {
            status: 422,
            detail:
              "The list contains a network that cannot be routed directly (private, reserved or larger than /12)",
          },
        },
      });
      const dialog = await open();
      await userEvent.upload(within(dialog).getByLabelText("Файл со списком"), file("10.0.0.0/8\n"));
      await userEvent.click(within(dialog).getByRole("button", { name: "Загрузить" }));
      expect(
        await within(dialog).findByText(/В списке есть частные, зарезервированные сети/),
      ).toBeInTheDocument();
      expect(within(dialog).queryByText(/cannot be routed directly/)).not.toBeInTheDocument();
    });

    it("explains a 413", async () => {
      setup({
        fail: {
          "POST /api/v1/geoip/upload": { status: 413, detail: "The list is larger than 5242880 bytes" },
        },
      });
      const dialog = await open();
      await userEvent.upload(within(dialog).getByLabelText("Файл со списком"), file());
      await userEvent.click(within(dialog).getByRole("button", { name: "Загрузить" }));
      expect(await within(dialog).findByText("Файл слишком большой.")).toBeInTheDocument();
    });
  });

  describe("exceptions", () => {
    it("lists them with the direction and the note", async () => {
      setup({
        overrides: [
          overrideDirect,
          { ...overrideDirect, id: "o2", network: "1.2.3.0/24", action: "tunnel", note: null },
        ],
      });
      const direct = (await screen.findByText("5.45.200.0/24")).closest("tr")!;
      expect(within(direct).getByText("напрямую")).toBeInTheDocument();
      expect(within(direct).getByText("Яндекс за CDN")).toBeInTheDocument();
      expect(
        within((await screen.findByText("1.2.3.0/24")).closest("tr")!).getByText("через выход"),
      ).toBeInTheDocument();
    });

    async function openAdd() {
      await userEvent.click(await screen.findByRole("button", { name: "Добавить исключение" }));
      return screen.findByRole("dialog", { name: "Новое исключение" });
    }

    it("adds a direct exception with a note", async () => {
      const api = setup();
      const dialog = await openAdd();
      await userEvent.type(within(dialog).getByLabelText("Сеть (CIDR)"), "5.45.200.0/24");
      await userEvent.type(within(dialog).getByLabelText(/^Заметка/), "CDN");
      await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));

      await waitFor(() =>
        expect(api.bodies["POST /api/v1/geoip/overrides"]).toEqual([
          { network: "5.45.200.0/24", action: "direct", note: "CDN" },
        ]),
      );
      expect(await screen.findByText("5.45.200.0/24")).toBeInTheDocument();
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    });

    it("adds an exception that goes through an exit", async () => {
      const api = setup();
      const dialog = await openAdd();
      await userEvent.type(within(dialog).getByLabelText("Сеть (CIDR)"), "8.8.8.0/24");
      await userEvent.click(within(dialog).getByRole("radio", { name: "через выход" }));
      await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));
      await waitFor(() =>
        expect(api.bodies["POST /api/v1/geoip/overrides"]).toEqual([
          { network: "8.8.8.0/24", action: "tunnel", note: null },
        ]),
      );
    });

    it("rejects something that is not a network before asking the server", async () => {
      const api = setup();
      const dialog = await openAdd();
      await userEvent.type(within(dialog).getByLabelText("Сеть (CIDR)"), "5.45.200.7");
      await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));
      expect(await within(dialog).findByText(/Укажите сеть IPv4 в формате CIDR/)).toBeInTheDocument();
      expect(api.calls.some((call) => call.startsWith("POST /api/v1/geoip/overrides"))).toBe(false);
    });

    it.each([
      [
        "A direct network must be public (no private, reserved or multicast ranges) and not larger than /16",
        /только публичную сеть не крупнее \/16/,
      ],
      ["A tunnel network must not be larger than /8", /не может быть крупнее \/8/],
      [
        "A direct network must not cover the address of a gateway or an exit server",
        /накрывает адрес шлюза или сервера-выхода/,
      ],
      ["Network must be an IPv4 CIDR with the host bits zero", /лишние биты хоста/],
    ])("explains the 422 %s", async (detail, expected) => {
      setup({ fail: { "POST /api/v1/geoip/overrides": { status: 422, detail } } });
      const dialog = await openAdd();
      await userEvent.type(within(dialog).getByLabelText("Сеть (CIDR)"), "10.0.0.0/8");
      await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));
      expect(await within(dialog).findByText(expected)).toBeInTheDocument();
      expect(within(dialog).queryByText(detail)).not.toBeInTheDocument();
    });

    it("explains a duplicate", async () => {
      setup({
        fail: {
          "POST /api/v1/geoip/overrides": {
            status: 409,
            detail: "An override for this network already exists",
          },
        },
      });
      const dialog = await openAdd();
      await userEvent.type(within(dialog).getByLabelText("Сеть (CIDR)"), "5.45.200.0/24");
      await userEvent.click(within(dialog).getByRole("button", { name: "Добавить" }));
      expect(await within(dialog).findByText("Такое исключение уже есть.")).toBeInTheDocument();
    });

    it("deletes an exception after confirmation", async () => {
      const api = setup({ overrides: [overrideDirect] });
      await userEvent.click(await screen.findByRole("button", { name: "Удалить исключение 5.45.200.0/24" }));
      const dialog = await screen.findByRole("alertdialog", { name: "Удалить исключение 5.45.200.0/24?" });
      await userEvent.click(within(dialog).getByRole("button", { name: "Удалить исключение" }));
      await waitFor(() => expect(api.calls).toContain("DELETE /api/v1/geoip/overrides/o1"));
      await waitFor(() => expect(screen.queryByText("Яндекс за CDN")).not.toBeInTheDocument());
    });
  });
});

describe("installing and removing the gateway protocol", () => {
  const wg = { id: "p1", server_id: GW, type: "amnezia_wg", port: 51820 };
  const gateway = { id: "p-gw", server_id: GW, type: "amnezia_gateway", port: 51821 };

  it("a server with only AmneziaWG keeps its checklist logic and offers the gateway in maintenance", async () => {
    const api = setup({ protocols: [wg] });
    expect(await screen.findByText("порт 51820, UDP")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Подготовка сервера" })).not.toBeInTheDocument();

    await userEvent.click(await screen.findByRole("button", { name: "Установить шлюз" }));
    await waitFor(() => expect(api.bodies[`POST ${base}/protocol`]).toEqual([{ type: "amnezia_gateway" }]));
    await userEvent.click(screen.getByRole("tab", { name: "Задачи" }));
    expect(await screen.findByText("Установка протокола")).toBeInTheDocument();
  });

  it("a server with both protocols shows two ports and two separate removals", async () => {
    const api = setup({ protocols: [wg, gateway] });
    await screen.findByText("порт 51820, UDP");
    expect(screen.getAllByText("порт 51821, UDP")).toHaveLength(2); // «Обслуживание» и сводка шлюза
    expect(screen.queryByRole("button", { name: "Установить шлюз" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Подготовка сервера" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Настройки" }));
    await userEvent.click(screen.getByRole("button", { name: "Снять шлюз" }));
    await userEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Снять шлюз" }),
    );
    await waitFor(() => expect(api.calls).toContain(`DELETE ${base}/protocol?type=amnezia_gateway`));

    await userEvent.click(await screen.findByRole("button", { name: "Снять AmneziaWG" }));
    await userEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Снять протокол" }),
    );
    await waitFor(() => expect(api.calls).toContain(`DELETE ${base}/protocol`));
  });

  it("a server with only the gateway does not count the protocol step as done, and the port is the gateway's", async () => {
    setup({ protocols: [gateway] });
    const item = (await screen.findByText("Установить AmneziaWG")).closest("li")!;
    expect(item).toHaveAttribute("data-state", "next");
    expect(within(item).getByText(/Шлюз уже установлен на этом сервере/)).toBeInTheDocument();
    expect(within(item).queryByRole("button", { name: "Установить шлюз" })).not.toBeInTheDocument();
    // Установка и ключ - уже сделаны: шлюз доказывает, что Amnezia стоит.
    expect(screen.getByText("Установить Docker и Amnezia").closest("li")).toHaveAttribute(
      "data-state",
      "done",
    );

    // «Обслуживание» работает; порт один - у шлюза, и он не выдан за порт AmneziaWG.
    expect(screen.getAllByText("порт 51821, UDP")).toHaveLength(2); // «Обслуживание» и сводка шлюза
    expect(screen.queryByText("порт 51820, UDP")).not.toBeInTheDocument();
    expect(screen.queryByText("AmneziaVPN (приложение)")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Настройки" }));
    expect(screen.queryByRole("button", { name: "Снять AmneziaWG" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Снять шлюз" })).toBeInTheDocument();
  });

  it("installs the gateway from the protocol step with the gateway type in the body", async () => {
    const api = setup({ protocols: [] });
    const item = (await screen.findByText("Установить AmneziaWG")).closest("li")!;
    expect(item).toHaveAttribute("data-state", "next");

    await userEvent.click(within(item).getByRole("button", { name: "Установить шлюз" }));
    await waitFor(() => expect(api.bodies[`POST ${base}/protocol`]).toEqual([{ type: "amnezia_gateway" }]));
    expect(within(item).getByText("Устанавливаем протокол…")).toBeInTheDocument();
  });

  it("still installs the ordinary protocol from the same step without a body", async () => {
    const api = setup({ protocols: [] });
    const item = (await screen.findByText("Установить AmneziaWG")).closest("li")!;
    expect(within(item).getByRole("button", { name: "Установить шлюз" })).toBeInTheDocument();

    await userEvent.click(within(item).getByRole("button", { name: "Установить протокол" }));
    await waitFor(() => expect(api.bodies[`POST ${base}/protocol`]).toEqual([""]));
  });

  it("explains a refused gateway removal", async () => {
    setup(
      {
        protocols: [gateway],
        fail: { [`DELETE ${base}/protocol?type=amnezia_gateway`]: { status: 409, detail: "live clients" } },
      },
      "settings",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Снять шлюз" }));
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Снять шлюз" }));
    expect(
      await within(dialog).findByText(/Снять шлюз нельзя: на нём есть живые клиенты/),
    ).toBeInTheDocument();
  });
});

describe("gateway section in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("is translated", async () => {
    setup({
      exits: [exit1],
      assignments: [assignmentAlice],
      candidates: [candidateList],
      overrides: [overrideDirect],
    });
    expect(await screen.findByRole("heading", { name: "Gateway", level: 2 })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Exits" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Clients and exits" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Russia direct" })).toBeInTheDocument();
    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(await screen.findByText("A new list is waiting for confirmation.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add exit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Update from RIPE" })).toBeInTheDocument();
  });
});
