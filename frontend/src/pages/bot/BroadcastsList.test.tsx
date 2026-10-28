import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import {
  BROADCAST2_ID,
  BROADCAST_ID,
  EMPTY_TEXT,
  OPERATOR,
  SECRET_DETAIL,
  VIEWER,
  broadcast,
  conflict,
  mockBot,
  tooMany,
  type BotBackend,
} from "../../test/botApi";
import { json, renderWithProviders } from "../../test/render";
import { BroadcastsPage } from "./BroadcastsPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4). Список рассылок - часть `BroadcastsPage` (форма - в
 * BroadcastsForm.test.tsx), читает GET /bot-admin/broadcasts.
 * - Элемент списка (li, tr или article) на рассылку. Внутри: вид («Новость» / «Ссылки на конфиг»), состояние словом
 *   («Идёт» / «Завершена» / «Отменена» - по полю `state`: running / finished / cancelled), <progress> и счётчики.
 * - <progress>: value = sent + blocked + failed + skipped + cancelled (обработано), max = total; доступное имя
 *   содержит оба числа («Обработано 70 из 100»). Счётчики подписаны словами: «отправлено», «заблокировали»,
 *   «ошибки», «пропущено», «отменено» (число рядом с подписью, в любом порядке).
 * - «Отменить» - у рассылки в состоянии running и только при bot:manage; сразу POST /broadcasts/{id}/cancel, без
 *   подтверждения. Повторная отмена (409) - Notice без `detail`.
 * - Опрос: пока в списке есть рассылка в состоянии running, список перечитывается раз в 2 с
 *   (refetchInterval(query) -> 2000 / false); как только running нет - опрос прекращается; при уходе со страницы
 *   тоже.
 */

const BROADCASTS = "/api/v1/bot-admin/broadcasts";
const FINISHED = broadcast({
  id: BROADCAST2_ID,
  state: "finished",
  kind: "config",
  text: null,
  sent: 90,
  blocked: 4,
  failed: 4,
  skipped: 2,
});
const RUNNING = broadcast();

function setup(permissions: string[] = OPERATOR, backend: BotBackend = {}) {
  const api = mockBot({ permissions, broadcasts: [RUNNING, FINISHED], ...backend });
  const rendered = renderWithProviders(<BroadcastsPage />, { permissions });
  return { api, ...rendered };
}

/** Рассылка в списке: от её <progress> вверх до элемента списка. Ждём, пока список придёт. */
async function items() {
  const bars = await screen.findAllByRole("progressbar");
  return bars.map((bar) => bar.closest("li, tr, article") as HTMLElement);
}

/** Число рядом с подписью, в любом порядке: «отправлено 60», «60 отправлено», «Отправлено: 60». */
function hasCount(element: HTMLElement, label: string, n: number): boolean {
  const text = element.textContent ?? "";
  return new RegExp(`${label}\\D{0,4}\\b${n}\\b|\\b${n}\\b\\D{0,4}${label}`, "i").test(text);
}

/** Прокрутить поддельные часы: опрос TanStack Query идёт по setInterval. */
async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("broadcast list: what is shown", () => {
  it("shows the kind and the state of each broadcast in words", async () => {
    setup();
    const [running, finished] = await items();

    expect(running).toHaveTextContent("Новость");
    expect(running).toHaveTextContent(/идёт/i);
    expect(finished).toHaveTextContent("Ссылки на конфиг");
    expect(finished).toHaveTextContent(/завершена/i);
  });

  it("says «cancelled» for a cancelled broadcast", async () => {
    setup(OPERATOR, {
      broadcasts: [
        broadcast({ state: "cancelled", cancelled: 40, sent: 50, blocked: 5, failed: 3, skipped: 2 }),
      ],
    });
    const [item] = await items();

    expect(item).toHaveTextContent(/отменена/i);
  });

  it("draws a progress bar with the processed count out of the total", async () => {
    setup();
    const [running] = await items();

    const bar = within(running as HTMLElement).getByRole("progressbar") as HTMLProgressElement;
    expect(bar.value).toBe(70); // 60 + 5 + 3 + 2 + 0
    expect(bar.max).toBe(100);
    expect(bar).toHaveAccessibleName(/70\D+100/);
  });

  it("shows the five counters under words", async () => {
    setup();
    const [running] = await items();

    expect(hasCount(running as HTMLElement, "отправлено", 60)).toBe(true);
    expect(hasCount(running as HTMLElement, "заблокировали", 5)).toBe(true);
    expect(hasCount(running as HTMLElement, "ошибки", 3)).toBe(true);
    expect(hasCount(running as HTMLElement, "пропущено", 2)).toBe(true);
    expect(hasCount(running as HTMLElement, "отменено", 0)).toBe(true);
  });

  it("says that the list is empty before the first broadcast", async () => {
    setup(OPERATOR, { broadcasts: [] });

    expect(await screen.findByText(EMPTY_TEXT.broadcasts)).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});

describe("broadcast list: cancelling", () => {
  it("offers «Отменить» only for a broadcast in progress", async () => {
    setup();
    const [running, finished] = await items();

    expect(within(running as HTMLElement).getByRole("button", { name: "Отменить" })).toBeInTheDocument();
    expect(
      within(finished as HTMLElement).queryByRole("button", { name: "Отменить" }),
    ).not.toBeInTheDocument();
  });

  it("gives a viewer the list and the counters but no button", async () => {
    setup(VIEWER);
    const [running] = await items();

    expect(hasCount(running as HTMLElement, "отправлено", 60)).toBe(true);
    expect(screen.queryByRole("button", { name: "Отменить" })).not.toBeInTheDocument();
  });

  it("cancels on the click and shows the broadcast as cancelled", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    const [running] = await items();

    await user.click(within(running as HTMLElement).getByRole("button", { name: "Отменить" }));

    await waitFor(() => expect(api.count("POST", `${BROADCASTS}/${BROADCAST_ID}/cancel`)).toBe(1));
    await waitFor(async () => expect((await items())[0]).toHaveTextContent(/отменена/i));
    expect(
      within((await items())[0] as HTMLElement).queryByRole("button", { name: "Отменить" }),
    ).not.toBeInTheDocument();
  });

  it("explains a refusal without the backend detail", async () => {
    const user = userEvent.setup();
    setup(OPERATOR, { respond: { [`POST ${BROADCASTS}/{id}/cancel`]: () => conflict("already_finished") } });
    const [running] = await items();

    await user.click(within(running as HTMLElement).getByRole("button", { name: "Отменить" }));

    expect(await screen.findByRole("alert")).not.toHaveTextContent(SECRET_DETAIL);
  });

  it("tells to try again in N seconds on 429", async () => {
    const user = userEvent.setup();
    setup(OPERATOR, { respond: { [`POST ${BROADCASTS}/{id}/cancel`]: () => tooMany(10) } });
    const [running] = await items();

    await user.click(within(running as HTMLElement).getByRole("button", { name: "Отменить" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/слишком часто/i);
    expect(alert).toHaveTextContent(/через 10 с/);
  });
});

describe("broadcast list: polling progress", () => {
  const listCalls = (api: ReturnType<typeof mockBot>) => api.count("GET", BROADCASTS);

  /** Список меняется от вызова к вызову: сначала идёт, потом закончилась. */
  const progressing = (finishAt: number) => (_call: unknown, n: number) =>
    json([
      n >= finishAt
        ? broadcast({ state: "finished", sent: 92, blocked: 5, failed: 2, skipped: 1 })
        : broadcast({ sent: 10 * (n + 1) }),
    ]);

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it("asks again about every two seconds while a broadcast is in progress", async () => {
    const { api } = setup(OPERATOR, {
      respond: { [`GET ${BROADCASTS}`]: progressing(99) },
    });
    await items();
    const before = listCalls(api);

    await tick(6_100);

    const asked = listCalls(api) - before;
    expect(asked).toBeGreaterThanOrEqual(2);
    expect(asked).toBeLessThanOrEqual(4);
  });

  it("shows the new counters as they come", async () => {
    setup(OPERATOR, { respond: { [`GET ${BROADCASTS}`]: progressing(99) } });
    const [first] = await items();
    expect(hasCount(first as HTMLElement, "отправлено", 10)).toBe(true);

    await tick(2_100);

    await waitFor(async () =>
      expect(hasCount((await items())[0] as HTMLElement, "отправлено", 10)).toBe(false),
    );
  });

  it("stops asking once nothing is in progress any more", async () => {
    const { api } = setup(OPERATOR, { respond: { [`GET ${BROADCASTS}`]: progressing(2) } });
    await items();

    await tick(10_000);
    await waitFor(async () => expect((await items())[0]).toHaveTextContent(/завершена/i));
    const settled = listCalls(api);
    await tick(20_000);

    expect(listCalls(api)).toBe(settled);
  });

  it("does not poll at all when every broadcast is already over", async () => {
    const { api } = setup(OPERATOR, { broadcasts: [FINISHED] });
    await items();
    const before = listCalls(api);

    await tick(20_000);

    expect(listCalls(api)).toBe(before);
  });

  it("stops asking when the admin leaves the page", async () => {
    const { api, unmount } = setup(OPERATOR, { respond: { [`GET ${BROADCASTS}`]: progressing(99) } });
    await items();
    unmount();
    const before = listCalls(api);

    await tick(20_000);

    expect(listCalls(api)).toBe(before);
  });
});

describe("broadcast list in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("speaks English: states, counters and buttons have no Russian", async () => {
    const { container } = setup();
    await items();

    expect(container.textContent).not.toMatch(/[А-Яа-яЁё]/);
    expect(screen.getByRole("button", { name: /^Cancel/ })).toBeInTheDocument();
  });
});
