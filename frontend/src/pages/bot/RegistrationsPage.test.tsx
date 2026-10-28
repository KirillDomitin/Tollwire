import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import {
  EMPTY_TEXT,
  OPERATOR,
  SECRET_DETAIL,
  VIEWER,
  ago,
  conflict,
  mockBot,
  registration,
  tooMany,
  type BotBackend,
} from "../../test/botApi";
import { renderWithProviders } from "../../test/render";
import { RegistrationsPage } from "./RegistrationsPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4):
 * - `src/pages/bot/RegistrationsPage.tsx` экспортирует `RegistrationsPage` - раздел «Регистрации»
 *   (/bot/registrations). Читает GET /bot-admin/registrations[?status=].
 * - Строка (li или tr) на регистрацию, опознаётся по имени (`name`). Фильтр - обычный <select> с подписью «Статус»;
 *   значения опций: "" (все), payment_pending, provisioning, active, failed. Выбор шлёт GET ...?status=<значение>.
 * - У регистрации в статусе failed: <details> (summary «Подробности») с текстом `error` и кнопка «Повторить» при
 *   bot:manage (POST /registrations/{id}/retry, сразу, без подтверждения). У остальных - ни details, ни кнопки.
 *   У наблюдателя (только bot:read) кнопки нет, подробности ошибки видны.
 * - Ошибки действия - Notice без `detail`; 429 - «слишком часто, попробуйте через N с».
 */

const LIST = [
  registration({ id: "r-active", name: "Ivan Petrov", status: "active" }),
  registration({ id: "r-wait", name: "Wait Person", status: "payment_pending", finished_at: null }),
  registration({ id: "r-prov", name: "Prov Person", status: "provisioning", finished_at: null }),
  registration({
    id: "r-failed",
    name: "Fail Person",
    status: "failed",
    error: "SSH connection to US2 timed out",
    finished_at: ago(60),
  }),
];

function setup(permissions: string[] = OPERATOR, backend: BotBackend = {}) {
  const api = mockBot({ permissions, registrations: LIST, ...backend });
  const rendered = renderWithProviders(<RegistrationsPage />, { permissions });
  return { api, ...rendered };
}

const rowOf = async (name: string) => (await screen.findByText(name)).closest("li, tr") as HTMLElement;
const REGISTRATIONS = "/api/v1/bot-admin/registrations";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bot registrations: the list", () => {
  it("lists every registration, newest first as the backend sends them", async () => {
    setup();

    for (const name of ["Ivan Petrov", "Wait Person", "Prov Person", "Fail Person"]) {
      expect(await rowOf(name)).toBeInTheDocument();
    }
  });

  it("asks for all statuses by default", async () => {
    const { api } = setup();
    await rowOf("Ivan Petrov");

    expect(api.last("GET", REGISTRATIONS)?.query.get("status")).toBeNull();
    expect(screen.getByLabelText("Статус")).toHaveValue("");
  });
});

describe("bot registrations: the filter", () => {
  it("asks the backend for one status and shows only those registrations", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await rowOf("Ivan Petrov");

    await user.selectOptions(screen.getByLabelText("Статус"), "failed");

    await waitFor(() => expect(api.last("GET", REGISTRATIONS)?.query.get("status")).toBe("failed"));
    expect(await rowOf("Fail Person")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Ivan Petrov")).not.toBeInTheDocument());
    expect(screen.queryByText("Wait Person")).not.toBeInTheDocument();
  });

  it("goes back to everything when the empty option is chosen again", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await rowOf("Ivan Petrov");
    await user.selectOptions(screen.getByLabelText("Статус"), "failed");
    await waitFor(() => expect(screen.queryByText("Ivan Petrov")).not.toBeInTheDocument());

    await user.selectOptions(screen.getByLabelText("Статус"), "");

    expect(await rowOf("Ivan Petrov")).toBeInTheDocument();
    expect(api.last("GET", REGISTRATIONS)?.query.get("status")).toBeNull();
  });

  it("says that nothing matches when the chosen status has no registrations", async () => {
    const user = userEvent.setup();
    setup(OPERATOR, { registrations: [registration({ id: "r1", name: "Ivan Petrov" })] });
    await rowOf("Ivan Petrov");

    await user.selectOptions(screen.getByLabelText("Статус"), "failed");

    expect(await screen.findByText(EMPTY_TEXT.registrations)).toBeInTheDocument();
  });
});

describe("bot registrations: a failed registration", () => {
  it("keeps the error in a collapsed <details> until it is opened", async () => {
    const user = userEvent.setup();
    setup();
    const row = await rowOf("Fail Person");

    const details = within(row).getByText("SSH connection to US2 timed out").closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    await user.click(within(row).getByText(/Подробности/));
    expect(details).toHaveAttribute("open");
  });

  it("offers «Повторить» to an operator, only on the failed one", async () => {
    setup();

    expect(within(await rowOf("Fail Person")).getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    for (const name of ["Ivan Petrov", "Wait Person", "Prov Person"]) {
      const row = await rowOf(name);
      expect(within(row).queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
      expect(row.querySelector("details")).toBeNull();
    }
  });

  it("retries on the click and shows the registration as being issued again", async () => {
    const user = userEvent.setup();
    const { api } = setup();

    await user.click(within(await rowOf("Fail Person")).getByRole("button", { name: "Повторить" }));

    await waitFor(() => expect(api.count("POST", `${REGISTRATIONS}/r-failed/retry`)).toBe(1));
    await waitFor(() => {
      const row = screen.getByText("Fail Person").closest("li, tr") as HTMLElement;
      expect(within(row).queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
    });
    expect(screen.queryByText("SSH connection to US2 timed out")).not.toBeInTheDocument();
  });

  it("gives a viewer the error text but no button", async () => {
    setup(VIEWER);
    const row = await rowOf("Fail Person");

    expect(within(row).getByText("SSH connection to US2 timed out")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
  });

  it("explains a refusal of the retry without the backend detail", async () => {
    const user = userEvent.setup();
    setup(OPERATOR, { respond: { [`POST ${REGISTRATIONS}/{id}/retry`]: () => conflict("not_failed") } });

    await user.click(within(await rowOf("Fail Person")).getByRole("button", { name: "Повторить" }));

    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent(SECRET_DETAIL);
    expect(within(await rowOf("Fail Person")).getByRole("button", { name: "Повторить" })).toBeEnabled();
  });

  it("tells to try again in N seconds on 429", async () => {
    const user = userEvent.setup();
    setup(OPERATOR, { respond: { [`POST ${REGISTRATIONS}/{id}/retry`]: () => tooMany(20) } });

    await user.click(within(await rowOf("Fail Person")).getByRole("button", { name: "Повторить" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/слишком часто/i);
    expect(alert).toHaveTextContent(/через 20 с/);
  });
});

describe("bot registrations in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("speaks English: no Russian is left on the page", async () => {
    const { container } = setup();

    await screen.findByText("Fail Person");

    expect(container.textContent).not.toMatch(/[А-Яа-яЁё]/);
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
  });
});
