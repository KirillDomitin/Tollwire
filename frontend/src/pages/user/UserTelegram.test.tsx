import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import {
  OUTBOX_ID,
  SECRET_DETAIL,
  USER_ID,
  conflict,
  mockBot,
  tooMany,
  type BotBackend,
} from "../../test/botApi";
import { json, renderWithProviders } from "../../test/render";
import { UserPage } from "./UserPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4). Правки карточки пользователя (src/pages/user/UserPage.tsx):
 * - GET /vpn-users/{id} теперь отдаёт telegram_id, telegram_username, bot_blocked_at (VPNUserRead).
 * - Строка «Telegram: @ник» показывается, если есть telegram_id. При bot_blocked_at к ней добавляется
 *   «заблокировал бота ДД.ММ» (число и месяц по местному времени, в ru - «12.10»).
 * - Кнопка «Отправить ссылку в Telegram» - только при bot:manage и наличии telegram_id. При bot_blocked_at она
 *   неактивна, а объяснение («заблокировал бота…») входит в её описание (aria-describedby).
 * - Нажатие: POST /bot-admin/users/{id}/config-link -> 202 {outbox_id, status}; затем опрос
 *   GET /bot-admin/outbox/{outbox_id} раз в 2 с, пока status in (pending, claimed); итог - Notice на странице:
 *     sent    -> «Ссылка отправлена в Telegram»
 *     blocked -> «Человек заблокировал бота — ссылка не доставлена»
 *     failed  -> «Не удалось отправить ссылку»
 *   Пока идёт отправка, кнопка неактивна; после итога снова доступна (кроме заблокировавших бота).
 * - 409 с `reason` - свой текст на каждую причину: no_telegram, bot_blocked, not_configured, no_access,
 *   config_unavailable. 429 - «слишком часто, попробуйте через N с». Любая другая ошибка - общий текст.
 *   `ApiError.detail` не показывается нигде.
 */

const CARD = ["vpn_users:read", "vpn_users:manage", "access:read", "access:manage", "bot:read", "bot:manage"];
const CARD_VIEWER = ["vpn_users:read", "access:read", "bot:read"];
const CONFIG_LINK = `/api/v1/bot-admin/users/${USER_ID}/config-link`;
const OUTBOX = `/api/v1/bot-admin/outbox/${OUTBOX_ID}`;
const BUTTON = "Отправить ссылку в Telegram";

function setup(permissions: string[] = CARD, backend: BotBackend = {}) {
  const api = mockBot({ permissions, ...backend });
  const rendered = renderWithProviders(<UserPage />, {
    route: `/users/${USER_ID}`,
    path: "/users/:userId",
    permissions,
  });
  return { api, ...rendered };
}

const outboxIs =
  (...statuses: string[]) =>
  (_call: unknown, n: number) =>
    json({ id: OUTBOX_ID, status: statuses[Math.min(n, statuses.length - 1)], error: null });

async function card() {
  await screen.findByRole("heading", { level: 1, name: "alice" });
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("user card: Telegram line", () => {
  it("shows @nick of the person", async () => {
    setup();

    const line = await screen.findByText(/@ivan_tg/);

    expect(line).toHaveTextContent(/Telegram/);
  });

  it("says when the person blocked the bot, with day and month", async () => {
    setup(CARD, { vpnUser: { bot_blocked_at: "2026-10-12T12:00:00Z" } });

    expect(await screen.findByText(/заблокировал бота 12\.10/)).toBeInTheDocument();
  });

  it("says nothing about blocking for a person who did not block the bot", async () => {
    setup();
    await screen.findByText(/@ivan_tg/);

    expect(screen.queryByText(/заблокировал/i)).not.toBeInTheDocument();
  });

  it("has no Telegram line and no button for a person without Telegram", async () => {
    setup(CARD, { vpnUser: { telegram_id: null, telegram_username: null } });
    await card();

    expect(screen.queryByText(/@ivan_tg/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Telegram/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: BUTTON })).not.toBeInTheDocument();
  });

  it("shows a person's Telegram line to a viewer, without the button", async () => {
    setup(CARD_VIEWER);

    expect(await screen.findByText(/@ivan_tg/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: BUTTON })).not.toBeInTheDocument();
  });
});

describe("user card: the «send link» button", () => {
  it("is offered to whoever may manage the bot, for a person with Telegram", async () => {
    setup();

    expect(await screen.findByRole("button", { name: BUTTON })).toBeEnabled();
  });

  it("is disabled, with the reason as its description, for a person who blocked the bot", async () => {
    setup(CARD, { vpnUser: { bot_blocked_at: "2026-10-12T12:00:00Z" } });

    const button = await screen.findByRole("button", { name: BUTTON });

    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(/заблокировал/i);
  });

  it("sends nothing until it is pressed", async () => {
    const { api } = setup();
    await screen.findByRole("button", { name: BUTTON });

    expect(api.count("POST", CONFIG_LINK)).toBe(0);
  });
});

describe("user card: sending the link", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  const press = async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(await screen.findByRole("button", { name: BUTTON }));
  };

  it("queues the link and asks about the message until there is an outcome", async () => {
    const { api } = setup(CARD, { respond: { [`GET ${OUTBOX}`]: outboxIs("pending", "claimed", "sent") } });

    await press();
    await waitFor(() => expect(api.count("POST", CONFIG_LINK)).toBe(1));
    for (let i = 0; i < 5; i += 1) await tick(2_000);

    expect(await screen.findByText(/ссылка отправлена/i)).toBeInTheDocument();
    expect(api.count("GET", OUTBOX)).toBeGreaterThanOrEqual(3);
  });

  it("keeps the button disabled while the message is on its way, then enables it again", async () => {
    setup(CARD, { respond: { [`GET ${OUTBOX}`]: outboxIs("pending", "pending", "sent") } });

    await press();

    await waitFor(() => expect(screen.getByRole("button", { name: BUTTON })).toBeDisabled());
    for (let i = 0; i < 5; i += 1) await tick(2_000);
    await screen.findByText(/ссылка отправлена/i);
    expect(screen.getByRole("button", { name: BUTTON })).toBeEnabled();
  });

  it("asks about the message about every two seconds, not more often", async () => {
    const { api } = setup(CARD, { respond: { [`GET ${OUTBOX}`]: outboxIs("pending") } });

    await press();
    await waitFor(() => expect(api.count("POST", CONFIG_LINK)).toBe(1));
    await tick(6_100);

    expect(api.count("GET", OUTBOX)).toBeGreaterThanOrEqual(2);
    expect(api.count("GET", OUTBOX)).toBeLessThanOrEqual(5);
  });

  it("stops asking once there is an outcome", async () => {
    const { api } = setup(CARD, { respond: { [`GET ${OUTBOX}`]: outboxIs("pending", "sent") } });

    await press();
    for (let i = 0; i < 4; i += 1) await tick(2_000);
    await screen.findByText(/ссылка отправлена/i);
    const settled = api.count("GET", OUTBOX);
    await tick(20_000);

    expect(api.count("GET", OUTBOX)).toBe(settled);
  });

  it("stops asking when the admin leaves the card", async () => {
    const { api, unmount } = setup(CARD, { respond: { [`GET ${OUTBOX}`]: outboxIs("pending") } });

    await press();
    await waitFor(() => expect(api.count("POST", CONFIG_LINK)).toBe(1));
    unmount();
    const before = api.count("GET", OUTBOX);
    await tick(20_000);

    expect(api.count("GET", OUTBOX)).toBe(before);
  });

  it("tells that the person blocked the bot when that is the outcome", async () => {
    setup(CARD, { respond: { [`GET ${OUTBOX}`]: outboxIs("pending", "blocked") } });

    await press();
    for (let i = 0; i < 4; i += 1) await tick(2_000);

    expect(await screen.findByText(/заблокировал бота.*не доставлена/i)).toBeInTheDocument();
    expect(screen.queryByText(/ссылка отправлена/i)).not.toBeInTheDocument();
  });

  it("tells that sending failed when that is the outcome, without any detail", async () => {
    setup(CARD, {
      respond: {
        [`GET ${OUTBOX}`]: () => json({ id: OUTBOX_ID, status: "failed", error: "chat_not_found" }),
      },
    });

    await press();
    for (let i = 0; i < 3; i += 1) await tick(2_000);

    expect(await screen.findByText(/не удалось отправить ссылку/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("chat_not_found");
  });

  it("does not show the backend detail when asking about the message fails", async () => {
    setup(CARD, { respond: { [`GET ${OUTBOX}`]: () => json({ detail: SECRET_DETAIL }, 500) } });

    await press();
    for (let i = 0; i < 3; i += 1) await tick(2_000);

    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent(SECRET_DETAIL);
  });
});

describe("user card: refusals to send the link", () => {
  async function refusedWith(response: () => Response): Promise<string> {
    const user = userEvent.setup();
    const view = setup(CARD, { respond: { [`POST ${CONFIG_LINK}`]: response } });
    await user.click(await screen.findByRole("button", { name: BUTTON }));
    const alert = await screen.findByRole("alert");
    const text = alert.textContent ?? "";
    view.unmount();
    vi.unstubAllGlobals();
    return text;
  }

  it("gives each 409 reason its own words", async () => {
    const reasons = ["no_telegram", "bot_blocked", "not_configured", "no_access", "config_unavailable"];
    const texts = new Map<string, string>();
    for (const reason of reasons) texts.set(reason, await refusedWith(() => conflict(reason)));

    expect(new Set(texts.values()).size).toBe(reasons.length);
    for (const [reason, text] of texts) {
      expect(text, reason).not.toContain(SECRET_DETAIL);
      expect(text, reason).not.toContain(reason);
    }
    expect(texts.get("bot_blocked")).toMatch(/заблокировал/i);
  });

  it("tells to try again in N seconds on 429", async () => {
    const text = await refusedWith(() => tooMany(20));

    expect(text).toMatch(/слишком часто/i);
    expect(text).toMatch(/через 20 с/);
  });

  it("shows a general text, never the detail, on a server error", async () => {
    const text = await refusedWith(() => json({ detail: SECRET_DETAIL }, 500));

    expect(text).not.toContain(SECRET_DETAIL);
    expect(text.length).toBeGreaterThan(0);
  });

  it("leaves the button usable after a refusal other than «blocked»", async () => {
    const user = userEvent.setup();
    setup(CARD, { respond: { [`POST ${CONFIG_LINK}`]: () => conflict("no_access") } });
    await user.click(await screen.findByRole("button", { name: BUTTON }));
    await screen.findByRole("alert");

    expect(screen.getByRole("button", { name: BUTTON })).toBeEnabled();
  });
});

describe("user card in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("speaks English in the Telegram line, the button and the reason it is disabled", async () => {
    const { container } = setup(CARD, { vpnUser: { bot_blocked_at: "2026-10-12T12:00:00Z" } });

    const button = await screen.findByRole("button", { name: /Telegram/ });

    expect(button).toBeDisabled();
    expect(container.textContent).not.toMatch(/[А-Яа-яЁё]/);
    expect(button.getAttribute("aria-describedby")).toBeTruthy();
    expect(button).toHaveAccessibleDescription(/blocked/i);
  });
});
