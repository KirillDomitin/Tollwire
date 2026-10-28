import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import { localDay } from "../../lib/time";
import {
  INVITE_CODE,
  INVITE_LINK,
  OPERATOR,
  PROTO2_ID,
  PROTO_ID,
  SECRET_DETAIL,
  USER_ID,
  VIEWER,
  ago,
  botSettings,
  cachedText,
  conflict,
  invite,
  mockBot,
  tooMany,
  type BotBackend,
} from "../../test/botApi";
import { json, renderWithProviders } from "../../test/render";
import { InvitesPage } from "./InvitesPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4):
 * - `src/pages/bot/InvitesPage.tsx` экспортирует `InvitesPage` - раздел «Приглашения» (/bot/invites). Читает
 *   GET /bot-admin/invites, /settings и /protocols.
 * - Список: строка (li или tr) на приглашение; опознаётся по заметке (`note`). Статус словами:
 *   valid «Ждёт», used «Использовано», expired «Истекло», revoked «Отозвано». У использованного имя
 *   (`used_by_name`) - ссылка на /users/{used_by_user_id}. «Отозвать» - только у valid и только при bot:manage,
 *   без подтверждения: сразу POST /invites/{id}/revoke.
 * - «Создать приглашение» (bot:manage) открывает окно (role="dialog") с полями:
 *     «Протокол» (select; value опции = id протокола; подставлен default_protocol_id из настроек),
 *     «Срок доступа, дней» (число; подставлен default_days),
 *     «Приглашение действует» (select с опциями «1 день», «7 дней», «30 дней», «Своя дата»; подставлено
 *       invite_valid_days дней), при «Своя дата» появляется поле даты «Дата» (type=date),
 *     «Заметка» (необязательно); кнопка «Создать».
 *   Тело POST /invites: { protocol_id, days, expires_at (ISO), note? } - значения подставлены явно.
 * - Результат создаётся в том же окне: поле только для чтения (как на /c) со ссылкой t.me из ответа (`link`), кнопка
 *   «Скопировать», пометка «показан один раз». Если `link` = null (имя бота неизвестно) - в поле код приглашения
 *   и подсказка про имя бота. Не вышло скопировать - «скопируйте … вручную». Закрытие окна (Esc) стирает ссылку и
 *   код из DOM и из кэша (gcTime: 0).
 * - Ошибки формы - Notice внутри окна, текст по статусу и `reason`, без `detail`: 409 no_default_protocol
 *   говорит про протокол и настройки; 429 - «слишком часто, попробуйте через N с» (N из Retry-After).
 */

const DAY = 86_400_000;

const LIST = [
  invite({ id: "i-valid", note: "n valid", status: "valid" }),
  invite({
    id: "i-used",
    note: "n used",
    status: "used",
    used_at: ago(3600),
    used_by_user_id: USER_ID,
    used_by_name: "Ivan Petrov",
  }),
  invite({ id: "i-expired", note: "n expired", status: "expired", expires_at: ago(3600) }),
  invite({ id: "i-revoked", note: "n revoked", status: "revoked", revoked_at: ago(1800) }),
];

function setup(permissions: string[] = OPERATOR, backend: BotBackend = {}) {
  const api = mockBot({ permissions, invites: LIST, ...backend });
  const rendered = renderWithProviders(<InvitesPage />, { permissions });
  return { api, ...rendered };
}

const rowOf = async (note: string) => (await screen.findByText(note)).closest("li, tr") as HTMLElement;

async function openForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /Создать приглашение/ }));
  const dialog = await screen.findByRole("dialog");
  // Протоколы и настройки приходят отдельными запросами: ждём, пока форма заполнится.
  await waitFor(() => expect(within(dialog).getByLabelText(/^Протокол/)).toHaveDisplayValue(/US2/));
  return dialog;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bot invites: the list", () => {
  it.each([
    ["n valid", /ждёт/i],
    ["n used", /использовано/i],
    ["n expired", /истекло/i],
    ["n revoked", /отозвано/i],
  ])("shows the status of %s in words", async (note, status) => {
    setup();

    expect(await rowOf(note)).toHaveTextContent(status);
  });

  it("names the person of a used invite with a link to their card", async () => {
    setup();

    const row = await rowOf("n used");

    expect(within(row).getByRole("link", { name: "Ivan Petrov" })).toHaveAttribute(
      "href",
      `/users/${USER_ID}`,
    );
  });

  it("lets an operator revoke only an invite that still waits", async () => {
    setup();

    expect(within(await rowOf("n valid")).getByRole("button", { name: /Отозвать/ })).toBeInTheDocument();
    for (const note of ["n used", "n expired", "n revoked"]) {
      expect(within(await rowOf(note)).queryByRole("button", { name: /Отозвать/ })).not.toBeInTheDocument();
    }
  });

  it("revokes at once and shows the invite as revoked", async () => {
    const user = userEvent.setup();
    const { api } = setup();

    await user.click(within(await rowOf("n valid")).getByRole("button", { name: /Отозвать/ }));

    await waitFor(() => expect(api.count("POST", "/api/v1/bot-admin/invites/i-valid/revoke")).toBe(1));
    await waitFor(() => expect(screen.getByText("n valid").closest("li, tr")).toHaveTextContent(/отозвано/i));
  });

  it("shows a viewer the list without any button", async () => {
    setup(VIEWER);

    await rowOf("n valid");

    expect(screen.queryByRole("button", { name: /Создать приглашение/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Отозвать/ })).not.toBeInTheDocument();
  });

  it("never shows a code or a link in the list", async () => {
    setup();

    await rowOf("n valid");

    expect(document.body.textContent).not.toContain(INVITE_CODE);
    expect(document.body.textContent).not.toContain("t.me");
  });
});

describe("bot invites: the form", () => {
  it("fills the protocol and the terms from the settings", async () => {
    const user = userEvent.setup();
    setup();

    const dialog = await openForm(user);

    expect(within(dialog).getByLabelText(/^Протокол/)).toHaveValue(PROTO_ID);
    expect(within(dialog).getByLabelText(/Срок доступа/)).toHaveValue(30);
    expect(within(dialog).getByLabelText(/Приглашение действует/)).toHaveDisplayValue("7 дней");
    expect(within(dialog).getByLabelText(/Заметка/)).toHaveValue("");
  });

  it("takes other defaults from the settings", async () => {
    const user = userEvent.setup();
    setup(OPERATOR, {
      settings: botSettings({ default_protocol_id: PROTO2_ID, default_days: 14, invite_valid_days: 30 }),
    });
    await user.click(await screen.findByRole("button", { name: /Создать приглашение/ }));
    const dialog = await screen.findByRole("dialog");

    await waitFor(() => expect(within(dialog).getByLabelText(/^Протокол/)).toHaveDisplayValue(/DE1/));
    expect(within(dialog).getByLabelText(/Срок доступа/)).toHaveValue(14);
    expect(within(dialog).getByLabelText(/Приглашение действует/)).toHaveDisplayValue("30 дней");
  });

  it("sends the substituted values as they are", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    const dialog = await openForm(user);

    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));

    await waitFor(() => expect(api.count("POST", "/api/v1/bot-admin/invites")).toBe(1));
    const body = api.last("POST", "/api/v1/bot-admin/invites")?.body as Record<string, unknown>;
    expect(body).toMatchObject({ protocol_id: PROTO_ID, days: 30 });
    const left = Date.parse(String(body.expires_at)) - Date.now();
    expect(left).toBeGreaterThan(6 * DAY);
    expect(left).toBeLessThan(9 * DAY);
  });

  it("sends what the admin chose: another protocol, days, validity and a note", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    const dialog = await openForm(user);

    await user.selectOptions(within(dialog).getByLabelText(/^Протокол/), PROTO2_ID);
    await user.clear(within(dialog).getByLabelText(/Срок доступа/));
    await user.type(within(dialog).getByLabelText(/Срок доступа/), "14");
    await user.selectOptions(within(dialog).getByLabelText(/Приглашение действует/), "30 дней");
    await user.type(within(dialog).getByLabelText(/Заметка/), "for Anna");
    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));

    await waitFor(() => expect(api.count("POST", "/api/v1/bot-admin/invites")).toBe(1));
    const body = api.last("POST", "/api/v1/bot-admin/invites")?.body as Record<string, unknown>;
    expect(body).toMatchObject({ protocol_id: PROTO2_ID, days: 14, note: "for Anna" });
    const left = Date.parse(String(body.expires_at)) - Date.now();
    expect(left).toBeGreaterThan(29 * DAY);
    expect(left).toBeLessThan(32 * DAY);
  });

  it("accepts a date of the admin's own", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    const dialog = await openForm(user);
    const day = new Date(Date.now() + 60 * DAY);

    await user.selectOptions(within(dialog).getByLabelText(/Приглашение действует/), "Своя дата");
    fireEvent.change(await within(dialog).findByLabelText(/Дата/), { target: { value: localDay(day) } });
    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));

    await waitFor(() => expect(api.count("POST", "/api/v1/bot-admin/invites")).toBe(1));
    const body = api.last("POST", "/api/v1/bot-admin/invites")?.body as { expires_at: string };
    expect(localDay(new Date(body.expires_at))).toBe(localDay(day));
  });

  it("shows the new invite in the list afterwards", async () => {
    const user = userEvent.setup();
    setup();
    const dialog = await openForm(user);
    await user.type(within(dialog).getByLabelText(/Заметка/), "fresh one");

    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));
    await screen.findByDisplayValue(INVITE_LINK);
    await user.keyboard("{Escape}");

    expect(await rowOf("fresh one")).toHaveTextContent(/ждёт/i);
  });
});

describe("bot invites: the result is shown once", () => {
  async function create(backend: BotBackend = {}) {
    const user = userEvent.setup();
    const view = setup(OPERATOR, backend);
    const dialog = await openForm(user);
    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));
    return { user, dialog, ...view };
  }

  it("shows the t.me link in a read-only field with a warning that it is shown once", async () => {
    await create();

    const field = await screen.findByDisplayValue(INVITE_LINK);
    expect(field).toHaveAttribute("readonly");
    expect(screen.getByRole("dialog")).toHaveTextContent(/один раз/i);
  });

  it("copies the link", async () => {
    const { user } = await create();

    await user.click(await screen.findByRole("button", { name: /Скопировать/ }));

    expect(await navigator.clipboard.readText()).toBe(INVITE_LINK);
  });

  it("tells to copy by hand when the browser has no clipboard, and keeps the field", async () => {
    const { user } = await create();
    const field = await screen.findByDisplayValue(INVITE_LINK);
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));

    await user.click(screen.getByRole("button", { name: /Скопировать/ }));

    expect(await screen.findByText(/скопируйте .* вручную/i)).toBeInTheDocument();
    expect(field).toBeInTheDocument();
  });

  it("shows the code and a hint when the bot name is not known yet", async () => {
    await create({ inviteLink: null });

    const field = await screen.findByDisplayValue(INVITE_CODE);
    expect(field).toHaveAttribute("readonly");
    expect(screen.getByRole("dialog")).toHaveTextContent(/имя бота/i);
  });

  it("erases the link from the page and from every cache when the window is closed", async () => {
    const { user, queryClient } = await create();
    await screen.findByDisplayValue(INVITE_LINK);
    expect(cachedText(queryClient)).toContain(INVITE_CODE); // пока окно открыто, ответ нужен экрану

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(INVITE_CODE);
    expect(screen.queryByDisplayValue(INVITE_LINK)).not.toBeInTheDocument();
    await waitFor(() => expect(cachedText(queryClient)).not.toContain(INVITE_CODE));
  });
});

describe("bot invites: refusals", () => {
  async function submitWith(response: () => Response) {
    const user = userEvent.setup();
    setup(OPERATOR, { respond: { "POST /api/v1/bot-admin/invites": response } });
    const dialog = await openForm(user);
    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));
    return within(dialog).findByRole("alert");
  }

  it("says there is no protocol to use, in words, on no_default_protocol", async () => {
    const alert = await submitWith(() => conflict("no_default_protocol"));

    expect(alert).toHaveTextContent(/протокол/i);
    expect(alert).toHaveTextContent(/настройк/i);
    expect(alert).not.toHaveTextContent(SECRET_DETAIL);
  });

  it("does not show the backend detail of an invalid request", async () => {
    const alert = await submitWith(() => json({ detail: SECRET_DETAIL }, 422));

    expect(alert).not.toHaveTextContent(SECRET_DETAIL);
  });

  it("tells to try again in N seconds on 429", async () => {
    const alert = await submitWith(() => tooMany(30));

    expect(alert).toHaveTextContent(/слишком часто/i);
    expect(alert).toHaveTextContent(/через 30 с/);
  });

  it("does not show a link after a refusal", async () => {
    await submitWith(() => json({ detail: SECRET_DETAIL }, 500));

    expect(screen.queryByDisplayValue(INVITE_LINK)).not.toBeInTheDocument();
  });
});

describe("bot invites in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("speaks English in the list, the form and the result", async () => {
    const user = userEvent.setup();
    const { container } = setup();
    await screen.findByText("n valid");
    expect(container.textContent).not.toMatch(/[А-Яа-яЁё]/);

    await user.click(await screen.findByRole("button", { name: /Create invite|New invite/i }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByLabelText(/^Protocol/)).toHaveDisplayValue(/US2/));
    expect(dialog.textContent).not.toMatch(/[А-Яа-яЁё]/);
    await user.click(within(dialog).getByRole("button", { name: /^Create$/ }));

    await screen.findByDisplayValue(INVITE_LINK);
    expect(screen.getByRole("dialog").textContent).not.toMatch(/[А-Яа-яЁё]/);
  });
});
