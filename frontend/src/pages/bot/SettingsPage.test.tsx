import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import {
  OPERATOR,
  PROTO2_ID,
  PROTO_ID,
  SECRET_DETAIL,
  VIEWER,
  botSettings,
  mockBot,
  tooMany,
  type BotBackend,
} from "../../test/botApi";
import { json, renderWithProviders } from "../../test/render";
import { SettingsPage } from "./SettingsPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4):
 * - `src/pages/bot/SettingsPage.tsx` экспортирует `SettingsPage` - раздел «Настройки» (/bot/settings). Читает
 *   GET /bot-admin/settings и /bot-admin/protocols, сохраняет PUT /bot-admin/settings.
 * - Поля: «Протокол по умолчанию» (select; value опции = id протокола, опция с value "" - «не выбран»),
 *   «Срок по умолчанию, дней» (число), «Приглашение действует, дней» (число); кнопка «Сохранить» (bot:manage).
 *   Поля порога «панель недоступна» нет (решение Р5.1-B): в форме ровно два числовых поля, в теле PUT ровно три
 *   ключа: default_protocol_id, default_days, invite_valid_days.
 * - После успеха - сообщение «Сохранено». У наблюдателя (только bot:read) значения видны, кнопки нет.
 * - Ошибки - Notice без `detail`; 429 - «слишком часто, попробуйте через N с».
 */

function setup(permissions: string[] = OPERATOR, backend: BotBackend = {}) {
  const api = mockBot({ permissions, ...backend });
  const rendered = renderWithProviders(<SettingsPage />, { permissions });
  return { api, ...rendered };
}

const SETTINGS = "/api/v1/bot-admin/settings";
const protocolField = () => screen.getByLabelText(/Протокол по умолчанию/);
const daysField = () => screen.getByLabelText(/Срок по умолчанию/);
const validField = () => screen.getByLabelText(/Приглашение действует/);

async function loaded() {
  await waitFor(() => expect(protocolField()).toHaveDisplayValue(/US2/));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bot settings", () => {
  it("shows the current settings", async () => {
    setup();
    await loaded();

    expect(protocolField()).toHaveValue(PROTO_ID);
    expect(daysField()).toHaveValue(30);
    expect(validField()).toHaveValue(7);
  });

  it("offers the protocols the panel has, with the server name", async () => {
    setup();
    await loaded();

    const options = Array.from((protocolField() as HTMLSelectElement).options).map((option) => option.text);
    expect(options.some((text) => text.includes("US2"))).toBe(true);
    expect(options.some((text) => text.includes("DE1"))).toBe(true);
  });

  it("has no field for the «panel is down» threshold yet", async () => {
    setup();
    await loaded();

    expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
    expect(screen.queryByLabelText(/недоступн/i)).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("300")).not.toBeInTheDocument();
  });

  it("saves exactly the three settings the page shows", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await loaded();

    await user.selectOptions(protocolField(), PROTO2_ID);
    await user.clear(daysField());
    await user.type(daysField(), "14");
    await user.clear(validField());
    await user.type(validField(), "3");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(api.last("PUT", SETTINGS)?.body).toEqual({
      default_protocol_id: PROTO2_ID,
      default_days: 14,
      invite_valid_days: 3,
    });
  });

  it("sends only those three keys even when nothing was changed", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await loaded();

    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(Object.keys(api.last("PUT", SETTINGS)?.body as object).sort()).toEqual([
      "default_days",
      "default_protocol_id",
      "invite_valid_days",
    ]);
  });

  it("confirms that the settings are saved", async () => {
    const user = userEvent.setup();
    setup();
    await loaded();

    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText(/сохранено/i)).toBeInTheDocument();
  });

  it("can leave the default protocol empty", async () => {
    const user = userEvent.setup();
    const { api } = setup(OPERATOR, { settings: botSettings({ default_protocol_id: null }) });
    await waitFor(() => expect(protocolField()).toHaveValue(""));

    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(api.last("PUT", SETTINGS)?.body).toMatchObject({ default_protocol_id: null });
  });

  it("shows a viewer the values without a save button", async () => {
    setup(VIEWER);
    await loaded();

    expect(daysField()).toHaveValue(30);
    expect(screen.queryByRole("button", { name: "Сохранить" })).not.toBeInTheDocument();
  });
});

describe("bot settings: refusals", () => {
  async function saveWith(response: () => Response) {
    const user = userEvent.setup();
    setup(OPERATOR, { respond: { [`PUT ${SETTINGS}`]: response } });
    await loaded();
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    return screen.findByRole("alert");
  }

  it("does not show the backend detail of an invalid value (422)", async () => {
    const alert = await saveWith(() => json({ detail: SECRET_DETAIL }, 422));

    expect(alert).not.toHaveTextContent(SECRET_DETAIL);
    expect(screen.queryByText(/сохранено/i)).not.toBeInTheDocument();
  });

  it("does not show the backend detail of a server error", async () => {
    const alert = await saveWith(() => json({ detail: SECRET_DETAIL }, 500));

    expect(alert).not.toHaveTextContent(SECRET_DETAIL);
  });

  it("tells to try again in N seconds on 429", async () => {
    const alert = await saveWith(() => tooMany(15));

    expect(alert).toHaveTextContent(/слишком часто/i);
    expect(alert).toHaveTextContent(/через 15 с/);
  });
});

describe("bot settings in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("speaks English: no Russian is left on the page", async () => {
    const { container } = setup();
    await waitFor(() => expect(screen.getByLabelText(/Default protocol/i)).toHaveDisplayValue(/US2/));

    expect(container.textContent).not.toMatch(/[А-Яа-яЁё]/);
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });
});
