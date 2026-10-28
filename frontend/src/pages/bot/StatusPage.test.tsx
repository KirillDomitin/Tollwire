import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import {
  KEY2_ID,
  KEY_ID,
  KEY_SECRET,
  OPERATOR,
  SECRET_DETAIL,
  SUPERADMIN,
  VIEWER,
  ago,
  botKey,
  botStatus,
  cachedText,
  mockBot,
  tooMany,
  type BotBackend,
} from "../../test/botApi";
import { json, renderWithProviders } from "../../test/render";
import { StatusPage } from "./StatusPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4):
 * - `src/pages/bot/StatusPage.tsx` экспортирует `StatusPage` - раздел «Состояние» (маршрут /bot). Читает
 *   GET /api/v1/bot-admin/status и GET /api/v1/bot-admin/keys; права берёт из useAuth().can.
 * - Состояние бота: «@имя» (bot_username), версия (last_seen_version), «Последний опрос» с относительным временем.
 * - Предупреждения - Notice (role="alert"):
 *     stale = true                                  -> «Бот давно не выходил на связь»
 *     outbox.pending + outbox.claimed > 0 и last_claim_at старше 2 минут или null
 *                                                   -> «Бот не забирает очередь (обновите бота)»
 * - Ключи: список (имя, key_prefix, «Отозван» у отозванных). Кнопки только при bot:keys:
 *     «Создать ключ» -> окно (role="dialog") с полем «Название» и кнопкой «Создать»; после ответа в том же окне
 *     поле только для чтения с ключом, текст «показан один раз» и кнопка «Скопировать»;
 *     закрытие окна (Esc) стирает ключ из DOM и из кэша (useMutation с gcTime: 0, reset при закрытии);
 *     «Отозвать» у действующего ключа -> ConfirmDialog (role="alertdialog") «бот перестанет работать сразу»,
 *     кнопка действия начинается с «Отозвать»; POST /keys/{id}/revoke только после подтверждения.
 * - Загрузка, ошибка и пустой список ключей - в src/pages/bot/states.test.tsx.
 */

const OLD_KEY = botKey({ id: KEY2_ID, name: "old bot", key_prefix: "twb_OldK", revoked_at: ago(86400) });

function setup(permissions: string[] = SUPERADMIN, backend: BotBackend = {}) {
  const api = mockBot({ permissions, ...backend });
  const rendered = renderWithProviders(<StatusPage />, { permissions });
  return { api, ...rendered };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bot status", () => {
  it("shows the bot name, the version and the last poll", async () => {
    setup();

    // Имя, версия и время опроса есть и в строке ключа, поэтому getAll*: важно, что они показаны.
    expect((await screen.findAllByText("@tollwire_bot")).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/0\.2\.0/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Последний опрос/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\d+ секунд\S* назад/).length).toBeGreaterThan(0);
  });

  it("raises no alarm for a bot that polls and takes its queue", async () => {
    setup(SUPERADMIN, { status: botStatus({ outbox: { pending: 5, claimed: 0 }, last_claim_at: ago(5) }) });

    await screen.findAllByText("@tollwire_bot");
    await screen.findByText("prod bot");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("warns when the bot has not polled for too long (stale)", async () => {
    setup(SUPERADMIN, { status: botStatus({ stale: true, last_seen_at: ago(3600) }) });

    expect(await screen.findByRole("alert")).toHaveTextContent(/давно не выходил на связь/i);
  });

  it("says the bot does not take the queue when messages wait and the last claim is old", async () => {
    setup(SUPERADMIN, {
      status: botStatus({ outbox: { pending: 5, claimed: 0 }, last_claim_at: ago(3600) }),
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/бот не забирает очередь/i);
    expect(alert).toHaveTextContent(/обновите бота/i);
  });

  it("says so as well when the bot has never claimed anything but messages wait", async () => {
    setup(SUPERADMIN, { status: botStatus({ outbox: { pending: 0, claimed: 2 }, last_claim_at: null }) });

    expect(await screen.findByRole("alert")).toHaveTextContent(/бот не забирает очередь/i);
  });

  it("does not blame the bot for an old claim when the queue is empty", async () => {
    setup(SUPERADMIN, {
      status: botStatus({ outbox: { pending: 0, claimed: 0 }, last_claim_at: ago(86400) }),
    });

    await screen.findAllByText("@tollwire_bot");
    await screen.findByText("prod bot");

    expect(screen.queryByText(/бот не забирает очередь/i)).not.toBeInTheDocument();
  });

  it("shows both warnings when the bot is stale and the queue is not taken", async () => {
    setup(SUPERADMIN, {
      status: botStatus({ stale: true, outbox: { pending: 3, claimed: 0 }, last_claim_at: ago(3600) }),
    });

    await screen.findAllByRole("alert");

    expect(screen.getByText(/давно не выходил на связь/i)).toBeInTheDocument();
    expect(screen.getByText(/бот не забирает очередь/i)).toBeInTheDocument();
  });
});

describe("bot keys: the list", () => {
  it("lists keys with the name and the prefix, never the key itself", async () => {
    setup(SUPERADMIN, { keys: [botKey(), OLD_KEY] });

    const row = (await screen.findByText("prod bot")).closest("li, tr") as HTMLElement;
    expect(row).toHaveTextContent("twb_AbCd");
    expect(document.body.textContent).not.toContain(KEY_SECRET);
  });

  it("marks a revoked key and gives it no revoke button", async () => {
    setup(SUPERADMIN, { keys: [botKey(), OLD_KEY] });

    const revoked = (await screen.findByText("old bot")).closest("li, tr") as HTMLElement;
    expect(revoked).toHaveTextContent(/отозван/i);
    expect(within(revoked).queryByRole("button", { name: /Отозвать/ })).not.toBeInTheDocument();
    const live = screen.getByText("prod bot").closest("li, tr") as HTMLElement;
    expect(within(live).getByRole("button", { name: /Отозвать/ })).toBeInTheDocument();
  });

  it.each([
    ["an operator", OPERATOR],
    ["a viewer", VIEWER],
  ])("hides every key button from %s", async (_who, permissions) => {
    setup(permissions);

    await screen.findByText("prod bot");

    expect(screen.queryByRole("button", { name: /Создать ключ/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Отозвать/ })).not.toBeInTheDocument();
  });

  it("shows the create and revoke buttons to whoever holds bot:keys", async () => {
    setup(SUPERADMIN);

    expect(await screen.findByRole("button", { name: /Создать ключ/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Отозвать/ })).toBeInTheDocument();
  });
});

describe("bot keys: creating a key", () => {
  async function createKey(name = "second bot") {
    const user = userEvent.setup();
    const view = setup(SUPERADMIN);
    await user.click(await screen.findByRole("button", { name: /Создать ключ/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Название/), name);
    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));
    return { user, dialog, ...view };
  }

  it("sends the name and shows the key once, in a read-only field with a warning", async () => {
    const { api } = await createKey();

    const field = await screen.findByDisplayValue(KEY_SECRET);
    expect(field).toHaveAttribute("readonly");
    expect(screen.getByRole("dialog")).toHaveTextContent(/один раз/i);
    expect(api.last("POST", "/api/v1/bot-admin/keys")?.body).toEqual({ name: "second bot" });
  });

  it("copies the key", async () => {
    const { user } = await createKey();

    await user.click(await screen.findByRole("button", { name: /Скопировать/ }));

    expect(await navigator.clipboard.readText()).toBe(KEY_SECRET);
  });

  it("shows the new key in the list by its prefix only", async () => {
    const { user } = await createKey();
    await screen.findByDisplayValue(KEY_SECRET);

    await user.keyboard("{Escape}");

    const row = (await screen.findByText("second bot")).closest("li, tr") as HTMLElement;
    expect(row).toHaveTextContent("twb_test");
    expect(document.body.textContent).not.toContain(KEY_SECRET);
  });

  it("erases the key from the page when the window is closed", async () => {
    const { user } = await createKey();
    await screen.findByDisplayValue(KEY_SECRET);

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByDisplayValue(KEY_SECRET)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(KEY_SECRET);
  });

  it("leaves the key in no cache once the window is closed (gcTime 0)", async () => {
    const { user, queryClient } = await createKey();
    await screen.findByDisplayValue(KEY_SECRET);
    expect(cachedText(queryClient)).toContain(KEY_SECRET); // пока окно открыто, ключ нужен экрану

    await user.keyboard("{Escape}");

    await waitFor(() => expect(cachedText(queryClient)).not.toContain(KEY_SECRET));
  });

  it("opens an empty form again, not the key shown before", async () => {
    const { user } = await createKey();
    await screen.findByDisplayValue(KEY_SECRET);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Создать ключ/ }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText(/Название/)).toHaveValue("");
    expect(screen.queryByDisplayValue(KEY_SECRET)).not.toBeInTheDocument();
  });

  it("explains a refusal without the backend detail", async () => {
    const user = userEvent.setup();
    setup(SUPERADMIN, {
      respond: { "POST /api/v1/bot-admin/keys": () => json({ detail: SECRET_DETAIL }, 500) },
    });
    await user.click(await screen.findByRole("button", { name: /Создать ключ/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Название/), "second bot");

    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));

    expect(await within(dialog).findByRole("alert")).not.toHaveTextContent(SECRET_DETAIL);
    expect(screen.queryByDisplayValue(KEY_SECRET)).not.toBeInTheDocument();
  });

  it("tells to wait on 429", async () => {
    const user = userEvent.setup();
    setup(SUPERADMIN, { respond: { "POST /api/v1/bot-admin/keys": () => tooMany(30) } });
    await user.click(await screen.findByRole("button", { name: /Создать ключ/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Название/), "second bot");

    await user.click(within(dialog).getByRole("button", { name: /^Создать$/ }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/слишком часто/i);
  });
});

describe("bot keys: revoking a key", () => {
  it("asks first and says the bot stops working at once", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    const row = (await screen.findByText("prod bot")).closest("li, tr") as HTMLElement;

    await user.click(within(row).getByRole("button", { name: /Отозвать/ }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/бот перестанет работать сразу/i);
    expect(api.count("POST", `/api/v1/bot-admin/keys/${KEY_ID}/revoke`)).toBe(0);
  });

  it("revokes the key after the confirmation and shows it as revoked", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    const row = (await screen.findByText("prod bot")).closest("li, tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /Отозвать/ }));

    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /^Отозвать/ }));

    await waitFor(() => expect(api.count("POST", `/api/v1/bot-admin/keys/${KEY_ID}/revoke`)).toBe(1));
    await waitFor(() => expect(screen.getByText("prod bot").closest("li, tr")).toHaveTextContent(/отозван/i));
  });

  it("does nothing when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    const row = (await screen.findByText("prod bot")).closest("li, tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /Отозвать/ }));

    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.count("POST", `/api/v1/bot-admin/keys/${KEY_ID}/revoke`)).toBe(0);
  });
});

describe("bot status in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("speaks English: warnings, keys and buttons have no Russian left", async () => {
    const { container } = setup(SUPERADMIN, {
      status: botStatus({ stale: true, outbox: { pending: 5, claimed: 0 }, last_claim_at: ago(3600) }),
      keys: [botKey(), OLD_KEY],
    });

    await screen.findAllByText("@tollwire_bot");
    await screen.findByText("old bot");
    await screen.findAllByRole("alert");

    expect(container.textContent).not.toMatch(/[А-Яа-яЁё]/);
  });

  it("speaks English in the key dialog", async () => {
    const user = userEvent.setup();
    setup(SUPERADMIN);
    await user.click(await screen.findByRole("button", { name: /Create key|New key/i }));

    const dialog = await screen.findByRole("dialog");

    expect(dialog.textContent).not.toMatch(/[А-Яа-яЁё]/);
  });
});
