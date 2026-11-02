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
import { botKeys } from "./useBot";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4):
 * - `src/pages/bot/SettingsPage.tsx` экспортирует `SettingsPage` - раздел «Настройки» (/bot/settings). Читает
 *   GET /bot-admin/settings и /bot-admin/protocols, сохраняет PUT /bot-admin/settings.
 * - Поля: «Протокол по умолчанию» (select; value опции = id протокола, опция с value "" - «не выбран»),
 *   «Срок по умолчанию, дней» (число), «Приглашение действует, дней» (число); кнопка «Сохранить» (bot:manage).
 *   Этап 25.5 добавляет четвёртое поле - порог «Панель недоступна, минут» (1..1440; в теле PUT -
 *   `panel_down_after_seconds` в секундах, 60..86400) с подсказкой про бота на одном сервере с панелью.
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

  it("has the «panel is down» threshold in minutes (300 s = 5 min)", async () => {
    setup();
    await loaded();

    expect(screen.getAllByRole("spinbutton")).toHaveLength(3);
    expect(screen.getByLabelText(/Панель недоступна/i)).toHaveValue(5);
  });

  it("saves exactly the four settings the page shows", async () => {
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
      panel_down_after_seconds: 300,
    });
  });

  it("sends only those four keys even when nothing was changed", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await loaded();

    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(Object.keys(api.last("PUT", SETTINGS)?.body as object).sort()).toEqual([
      "default_days",
      "default_protocol_id",
      "invite_valid_days",
      "panel_down_after_seconds",
    ]);
  });

  it("sends the threshold in seconds", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await loaded();

    await user.clear(screen.getByLabelText(/Панель недоступна/i));
    await user.type(screen.getByLabelText(/Панель недоступна/i), "10");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(api.last("PUT", SETTINGS)?.body).toMatchObject({ panel_down_after_seconds: 600 });
  });

  it("uses refreshed pristine settings while preserving a locally edited field", async () => {
    const user = userEvent.setup();
    const { api, queryClient } = setup();
    await loaded();
    await user.clear(daysField());
    await user.type(daysField(), "14");
    api.world.settings = botSettings({ default_protocol_id: PROTO2_ID, default_days: 60, invite_valid_days: 3, panel_down_after_seconds: 90 });

    await queryClient.invalidateQueries({ queryKey: botKeys.settings });

    await waitFor(() => expect(protocolField()).toHaveValue(PROTO2_ID));
    expect(daysField()).toHaveValue(14);
    expect(validField()).toHaveValue(3);
    expect(screen.getByLabelText(/Панель недоступна/i)).toHaveValue(1.5);
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(api.last("PUT", SETTINGS)?.body).toEqual({ default_protocol_id: PROTO2_ID, default_days: 14, invite_valid_days: 3, panel_down_after_seconds: 90 });
  });

  it.each([90, 77])("preserves a stored threshold of %s seconds when saving another field", async (seconds) => {
    const user = userEvent.setup();
    const { api } = setup(OPERATOR, { settings: botSettings({ panel_down_after_seconds: seconds }) });
    await loaded();
    expect(screen.getByLabelText(/Панель недоступна/i)).toHaveValue(seconds / 60);
    await user.clear(daysField());
    await user.type(daysField(), "14");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(api.last("PUT", SETTINGS)?.body).toMatchObject({ default_days: 14, panel_down_after_seconds: seconds });
  });

  it("accepts fractional minutes that represent whole seconds", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await loaded();
    await user.clear(screen.getByLabelText(/Панель недоступна/i));
    await user.type(screen.getByLabelText(/Панель недоступна/i), "1.5");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(api.last("PUT", SETTINGS)?.body).toMatchObject({ panel_down_after_seconds: 90 });
  });

  it("refuses fractional seconds without sending a request", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await loaded();
    await user.clear(screen.getByLabelText(/Панель недоступна/i));
    await user.type(screen.getByLabelText(/Панель недоступна/i), "1.001");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(api.count("PUT", SETTINGS)).toBe(0);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it.each(["0", "1441"])("refuses a threshold of %s minutes without sending anything", async (minutes) => {
    const user = userEvent.setup();
    const { api } = setup();
    await loaded();

    await user.clear(screen.getByLabelText(/Панель недоступна/i));
    await user.type(screen.getByLabelText(/Панель недоступна/i), minutes);
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(api.count("PUT", SETTINGS)).toBe(0);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it.each([
    ["1", 60],
    ["1440", 86400],
  ])("accepts the edge of %s minutes", async (minutes, seconds) => {
    const user = userEvent.setup();
    const { api } = setup();
    await loaded();

    await user.clear(screen.getByLabelText(/Панель недоступна/i));
    await user.type(screen.getByLabelText(/Панель недоступна/i), minutes);
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.count("PUT", SETTINGS)).toBe(1));
    expect(api.last("PUT", SETTINGS)?.body).toMatchObject({ panel_down_after_seconds: seconds });
  });

  it("explains that a bot on the panel server cannot see the host going down", async () => {
    setup();
    await loaded();

    expect(screen.getByText(/на том же сервере/i)).toBeInTheDocument();
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
