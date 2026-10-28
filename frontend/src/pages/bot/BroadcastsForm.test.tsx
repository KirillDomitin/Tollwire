import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import {
  BROADCAST2_ID,
  EMPTY_TEXT,
  OPERATOR,
  PROTO2_ID,
  PROTO_ID,
  SECRET_DETAIL,
  USER_ID,
  VIEWER,
  broadcast,
  conflict,
  invalidHtml,
  mockBot,
  tooMany,
  type BotBackend,
} from "../../test/botApi";
import { json, renderWithProviders } from "../../test/render";
import { BroadcastsPage } from "./BroadcastsPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4). Форма и список рассылок - один экран `BroadcastsPage`
 * (src/pages/bot/BroadcastsPage.tsx, раздел «Рассылки», /bot/broadcasts); список и опрос - в BroadcastsList.test.tsx.
 * Форма показывается при bot:manage; наблюдатель видит только список.
 *
 * Вид рассылки: радио «Новость» (по умолчанию) и «Ссылки на конфиг».
 * Текст (только у новости): textarea с подписью «Текст новости»; счётчик «N из 4096» связан через aria-describedby
 *   (N - видимая длина в единицах UTF-16 после разбора тегов, эмодзи = 2); при ошибке проверки текст ошибки тоже
 *   в описании поля, aria-invalid="true", кнопка отправки неактивна.
 * Предпросмотр: region с заголовком «Предпросмотр», внутри - <TelegramPreview html={текст} />
 *   (src/pages/bot/TelegramPreview.tsx, см. TelegramPreview.test.tsx).
 * Кому: радио «Все с Telegram» / «По протоколу» / «Выбранные» (у «Ссылки на конфиг» радио «Все…» нет); при
 *   «По протоколу» - select «Протокол» (value опции = id протокола); при «Выбранные» - поле «Поиск получателей»,
 *   под ним чекбоксы людей (подпись: имя, @ник, «заблокировал бота» у заблокировавших).
 *   Число получателей: POST /bot-admin/broadcasts/audience {kind, target, protocol_id?, user_ids?} при каждом
 *   изменении выбора -> кнопка «Отправить N получателям» (ru: 1 -> «получателю», 21 -> «получателю»).
 * Отправка: кнопка -> ConfirmDialog (role="alertdialog") с числом получателей, кнопка действия начинается с
 *   «Отправить». Тело POST /bot-admin/broadcasts: {request_id (UUID, один на форму, переживает повтор после
 *   ошибки), kind, text?, target, protocol_id?, user_ids?}. Для ссылок на конфиг в подтверждении предупреждение,
 *   что прежние ссылки получателей перестанут работать.
 * Английский: «Send to N recipients» (1 -> «Send to 1 recipient»), подпись поля содержит «text».
 * Ошибки: Notice без `detail`. 422 telegram_html_{tag,attr,entity,nesting,length,empty} - свой текст на каждый
 *   код (те же тексты, что у проверки на странице); 429 - «слишком часто, попробуйте через N с»; 409 не заданного
 *   публичного адреса (reason not_configured) - свой текст.
 */

const BROADCASTS = "/api/v1/bot-admin/broadcasts";
const HTML_CODES = [
  "telegram_html_tag",
  "telegram_html_attr",
  "telegram_html_entity",
  "telegram_html_nesting",
  "telegram_html_length",
  "telegram_html_empty",
];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function setup(permissions: string[] = OPERATOR, backend: BotBackend = {}) {
  const api = mockBot({ permissions, ...backend });
  const rendered = renderWithProviders(<BroadcastsPage />, { permissions });
  return { api, ...rendered };
}

const textbox = () => screen.getByRole("textbox", { name: /Текст/ });
const sendButton = () => screen.getByRole("button", { name: /^Отправить/ });
const fill = (text: string) => fireEvent.change(textbox(), { target: { value: text } });
const readyForm = () => screen.findByRole("textbox", { name: /Текст/ });
const audienceOf = (recipients: number) => () => json({ recipients, without_telegram: 3, blocked: 2 });

/** Нажать «Отправить» (если подтверждения ещё нет на экране) и подтвердить. */
async function send(user: ReturnType<typeof userEvent.setup>) {
  if (!screen.queryByRole("alertdialog")) {
    await waitFor(() => expect(sendButton()).toBeEnabled());
    await user.click(sendButton());
  }
  const dialog = await screen.findByRole("alertdialog");
  await user.click(within(dialog).getByRole("button", { name: /^Отправить/ }));
  return dialog;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("broadcast form: what the admin sees first", () => {
  it("starts with a news broadcast to everyone, an empty text and nothing to send", async () => {
    setup();
    await readyForm();

    expect(screen.getByRole("radio", { name: "Новость" })).toBeChecked();
    expect(screen.getByRole("radio", { name: /^Все/ })).toBeChecked();
    expect(textbox()).toHaveValue("");
    expect(sendButton()).toBeDisabled();
  });

  it("gives a viewer no form at all", async () => {
    setup(VIEWER);

    await screen.findByText(EMPTY_TEXT.broadcasts); // список загрузился, формы в нём нет

    expect(screen.queryByRole("textbox", { name: /Текст/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Отправить/ })).not.toBeInTheDocument();
  });
});

describe("broadcast form: the counter", () => {
  it("is linked to the field and starts at zero", async () => {
    setup();
    await readyForm();

    expect(textbox()).toHaveAccessibleDescription(/0\s+из\s+4096/);
  });

  it("counts characters", async () => {
    setup();
    await readyForm();

    fill("Привет");

    expect(textbox()).toHaveAccessibleDescription(/6\s+из\s+4096/);
  });

  it("counts an emoji as two units, like Telegram does", async () => {
    setup();
    await readyForm();

    fill("😀");

    expect(textbox()).toHaveAccessibleDescription(/(^|\D)2\s+из\s+4096/);
  });

  it("counts the visible text, not the tags", async () => {
    setup();
    await readyForm();

    fill("<b>ab</b>");

    expect(textbox()).toHaveAccessibleDescription(/(^|\D)2\s+из\s+4096/);
  });

  it("shows 4097 of 4096, flags the field and blocks sending", async () => {
    setup();
    await readyForm();

    fill("a".repeat(4097));

    expect(textbox()).toHaveAccessibleDescription(/4097\s+из\s+4096/);
    expect(textbox()).toHaveAttribute("aria-invalid", "true");
    expect(sendButton()).toBeDisabled();
  });

  it("lets 4096 units through", async () => {
    setup();
    await readyForm();

    fill("a".repeat(4096));

    expect(textbox()).toHaveAccessibleDescription(/4096\s+из\s+4096/);
    expect(textbox()).not.toHaveAttribute("aria-invalid", "true");
    await waitFor(() => expect(sendButton()).toBeEnabled());
  });
});

describe("broadcast form: the preview", () => {
  const preview = () => screen.getByRole("region", { name: /Предпросмотр/ });

  it("shows the formatted text", async () => {
    setup();
    await readyForm();

    fill("<b>Важно</b>: завтра работы");

    expect(preview().querySelector("b, strong")).toHaveTextContent("Важно");
    expect(preview()).toHaveTextContent("Важно: завтра работы");
  });

  it("prints <img onerror> as text and creates no element", async () => {
    setup();
    await readyForm();
    const hostile = '<img src=x onerror="alert(1)">';

    fill(hostile);

    expect(preview().querySelector("img")).toBeNull();
    expect(document.querySelector("img[onerror]")).toBeNull();
    expect(preview().textContent).toContain(hostile);
  });

  it("never writes the typed text through innerHTML", async () => {
    const setter = vi.spyOn(Element.prototype, "innerHTML", "set");
    setup();
    await readyForm();

    fill('<img src=x onerror="alert(1)"><b>x</b>');

    expect(
      setter.mock.calls.map((call) => String(call[0])).filter((text) => /onerror|<img/i.test(text)),
    ).toEqual([]);
    setter.mockRestore();
  });

  it("does not make links clickable", async () => {
    setup();
    await readyForm();

    fill('Читайте <a href="https://example.com/news">новости</a>');

    expect(preview()).toHaveTextContent("новости");
    expect(within(preview()).queryByRole("link")).not.toBeInTheDocument();
    expect(preview().querySelector("a, [href]")).toBeNull();
  });

  it("flags a text Telegram would refuse, saying what is wrong, and does not let it be sent", async () => {
    setup();
    await readyForm();

    fill("<script>alert(1)</script>");

    expect(textbox()).toHaveAttribute("aria-invalid", "true");
    expect(textbox()).toHaveAccessibleDescription(/тег/i);
    expect(sendButton()).toBeDisabled();
  });
});

describe("broadcast form: the number of recipients", () => {
  it("asks the panel who would get it, and shows the number on the button", async () => {
    const { api } = setup();
    await readyForm();

    fill("Привет");

    expect(await screen.findByRole("button", { name: "Отправить 12 получателям" })).toBeEnabled();
    expect(api.last("POST", `${BROADCASTS}/audience`)?.body).toMatchObject({ kind: "news", target: "all" });
  });

  it.each([
    [1, "Отправить 1 получателю"],
    [12, "Отправить 12 получателям"],
    [21, "Отправить 21 получателю"],
  ])("declines the word for %i recipients", async (count, name) => {
    setup(OPERATOR, { respond: { [`POST ${BROADCASTS}/audience`]: audienceOf(count) } });
    await readyForm();

    fill("Привет");

    expect(await screen.findByRole("button", { name })).toBeEnabled();
  });

  it("does not let an empty audience be sent", async () => {
    setup(OPERATOR, { respond: { [`POST ${BROADCASTS}/audience`]: audienceOf(0) } });
    await readyForm();

    fill("Привет");

    await waitFor(() => expect(sendButton()).toBeDisabled());
  });

  it("asks again for a protocol audience with the chosen protocol", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await readyForm();

    await user.click(screen.getByRole("radio", { name: /^По протоколу/ }));
    await user.selectOptions(await screen.findByLabelText(/^Протокол$/), PROTO2_ID);

    await waitFor(() =>
      expect(api.last("POST", `${BROADCASTS}/audience`)?.body).toMatchObject({
        target: "protocol",
        protocol_id: PROTO2_ID,
      }),
    );
  });
});

describe("broadcast form: choosing people", () => {
  async function openSelected(user: ReturnType<typeof userEvent.setup>, backend: BotBackend = {}) {
    const view = setup(OPERATOR, {
      respond: {
        [`POST ${BROADCASTS}/audience`]: (call) =>
          json({
            recipients: (call.body as { user_ids?: string[] }).user_ids?.length ?? 0,
            without_telegram: 0,
            blocked: 0,
          }),
      },
      ...backend,
    });
    await readyForm();
    await user.click(screen.getByRole("radio", { name: /^Выбранные/ }));
    return view;
  }

  it("searches people with Telegram as the admin types", async () => {
    const user = userEvent.setup();
    const { api } = await openSelected(user);
    await user.type(await screen.findByLabelText(/Поиск получателей/), "iva");

    await waitFor(() =>
      expect(
        api.of("GET", "/api/v1/bot-admin/recipients").some((call) => call.query.get("search") === "iva"),
      ).toBe(true),
    );
    expect(await screen.findByRole("checkbox", { name: /Ivan Petrov/ })).not.toBeChecked();
    expect(screen.queryByRole("checkbox", { name: /Anna Blocked/ })).not.toBeInTheDocument();
  });

  it("marks those who blocked the bot", async () => {
    const user = userEvent.setup();
    await openSelected(user);

    await user.type(await screen.findByLabelText(/Поиск получателей/), "anna");

    const person = await screen.findByRole("checkbox", { name: /Anna Blocked/ });
    expect(person).toHaveAccessibleName(/заблокировал/i);
  });

  it("counts the people ticked and sends exactly their ids", async () => {
    const user = userEvent.setup();
    const { api } = await openSelected(user);
    await user.type(await screen.findByLabelText(/Поиск получателей/), "iva");
    fill("Привет");

    await user.click(await screen.findByRole("checkbox", { name: /Ivan Petrov/ }));

    expect(await screen.findByRole("button", { name: "Отправить 1 получателю" })).toBeEnabled();
    expect(api.last("POST", `${BROADCASTS}/audience`)?.body).toMatchObject({
      target: "selected",
      user_ids: [USER_ID],
    });
    await send(user);
    await waitFor(() => expect(api.count("POST", BROADCASTS)).toBe(1));
    expect(api.last("POST", BROADCASTS)?.body).toMatchObject({ target: "selected", user_ids: [USER_ID] });
  });

  it("offers no one to send to before somebody is ticked", async () => {
    const user = userEvent.setup();
    await openSelected(user);
    fill("Привет");

    await waitFor(() => expect(sendButton()).toBeDisabled());
  });
});

describe("broadcast form: sending news", () => {
  it("asks first and names the number of people", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await readyForm();
    fill("Привет");

    await user.click(await screen.findByRole("button", { name: "Отправить 12 получателям" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("12");
    expect(dialog).not.toHaveTextContent(/прежн/i); // предупреждение о ссылках - только у ссылок на конфиг
    expect(api.count("POST", BROADCASTS)).toBe(0);
  });

  it("sends nothing when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await readyForm();
    fill("Привет");
    await user.click(await screen.findByRole("button", { name: "Отправить 12 получателям" }));

    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Отмена" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.count("POST", BROADCASTS)).toBe(0);
  });

  it("posts the text as typed, with a request id", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await readyForm();
    fill("<b>Привет</b>");
    await screen.findByRole("button", { name: "Отправить 12 получателям" });

    await send(user);

    await waitFor(() => expect(api.count("POST", BROADCASTS)).toBe(1));
    const body = api.last("POST", BROADCASTS)?.body as Record<string, unknown>;
    expect(body).toMatchObject({ kind: "news", text: "<b>Привет</b>", target: "all" });
    expect(String(body.request_id)).toMatch(UUID);
    expect(body).not.toHaveProperty("user_ids");
  });

  it("posts the chosen protocol for a protocol audience", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await readyForm();
    fill("Привет");
    await user.click(screen.getByRole("radio", { name: /^По протоколу/ }));
    await user.selectOptions(await screen.findByLabelText(/^Протокол$/), PROTO_ID);
    await screen.findByRole("button", { name: "Отправить 12 получателям" });

    await send(user);

    await waitFor(() => expect(api.count("POST", BROADCASTS)).toBe(1));
    expect(api.last("POST", BROADCASTS)?.body).toMatchObject({ target: "protocol", protocol_id: PROTO_ID });
  });

  it("closes the confirmation and shows the new broadcast in the list", async () => {
    const user = userEvent.setup();
    setup();
    await readyForm();
    fill("Привет");
    await screen.findByRole("button", { name: "Отправить 12 получателям" });
    expect(screen.queryAllByRole("progressbar")).toHaveLength(0);

    await send(user);

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(await screen.findAllByRole("progressbar")).toHaveLength(1);
  });

  it("repeats the same request id when the admin tries again after a failure", async () => {
    const user = userEvent.setup();
    const { api } = setup(OPERATOR, {
      respond: {
        [`POST ${BROADCASTS}`]: (_call, n) =>
          n === 0 ? json({ detail: SECRET_DETAIL }, 500) : json(broadcast({ id: BROADCAST2_ID }), 201),
      },
    });
    await readyForm();
    fill("Привет");
    await screen.findByRole("button", { name: "Отправить 12 получателям" });

    await send(user);
    await screen.findAllByRole("alert");
    await send(user);

    await waitFor(() => expect(api.count("POST", BROADCASTS)).toBe(2));
    const [first, second] = api
      .of("POST", BROADCASTS)
      .map((call) => (call.body as { request_id: string }).request_id);
    expect(first).toMatch(UUID);
    expect(second).toBe(first);
  });
});

describe("broadcast form: sending config links", () => {
  async function chooseConfig(user: ReturnType<typeof userEvent.setup>) {
    await readyForm();
    await user.click(screen.getByRole("radio", { name: "Ссылки на конфиг" }));
  }

  it("has no text field, no preview and no «all» audience", async () => {
    const user = userEvent.setup();
    setup();
    await chooseConfig(user);

    expect(screen.queryByRole("textbox", { name: /Текст/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Предпросмотр/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /^Все/ })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /^По протоколу/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /^Выбранные/ })).toBeInTheDocument();
  });

  it("warns in the confirmation that the previous links stop working", async () => {
    const user = userEvent.setup();
    setup();
    await chooseConfig(user);
    await user.click(screen.getByRole("radio", { name: /^По протоколу/ }));
    await user.selectOptions(await screen.findByLabelText(/^Протокол$/), PROTO_ID);

    await user.click(await screen.findByRole("button", { name: "Отправить 12 получателям" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("12");
    expect(dialog).toHaveTextContent(/прежн/i);
    expect(dialog).toHaveTextContent(/перестан|погас|не будут работать|больше не работа/i);
  });

  it("posts a config broadcast to a protocol without any text", async () => {
    const user = userEvent.setup();
    const { api } = setup();
    await chooseConfig(user);
    await user.click(screen.getByRole("radio", { name: /^По протоколу/ }));
    await user.selectOptions(await screen.findByLabelText(/^Протокол$/), PROTO_ID);
    await screen.findByRole("button", { name: "Отправить 12 получателям" });

    await send(user);

    await waitFor(() => expect(api.count("POST", BROADCASTS)).toBe(1));
    const body = api.last("POST", BROADCASTS)?.body as Record<string, unknown>;
    expect(body).toMatchObject({ kind: "config", target: "protocol", protocol_id: PROTO_ID });
    expect(body.text ?? null).toBeNull();
  });

  it("explains that the panel address is not set (409 not_configured) without the detail", async () => {
    const user = userEvent.setup();
    setup(OPERATOR, { respond: { [`POST ${BROADCASTS}`]: () => conflict("not_configured") } });
    await chooseConfig(user);
    await user.click(screen.getByRole("radio", { name: /^По протоколу/ }));
    await user.selectOptions(await screen.findByLabelText(/^Протокол$/), PROTO_ID);
    await screen.findByRole("button", { name: "Отправить 12 получателям" });

    await send(user);

    const alerts = await screen.findAllByRole("alert");
    const text = alerts.map((alert) => alert.textContent).join(" ");
    expect(text).not.toContain(SECRET_DETAIL);
    expect(text).toMatch(/адрес/i);
  });
});

describe("broadcast form: refusals of the backend", () => {
  async function rejectedWith(response: () => Response): Promise<string> {
    const user = userEvent.setup();
    const view = setup(OPERATOR, { respond: { [`POST ${BROADCASTS}`]: response } });
    await readyForm();
    fill("Привет");
    await screen.findByRole("button", { name: "Отправить 12 получателям" });
    await send(user);
    const alerts = await screen.findAllByRole("alert");
    const text = alerts.map((alert) => alert.textContent).join(" | ");
    view.unmount();
    vi.unstubAllGlobals();
    return text;
  }

  it("tells to try again in N seconds on 429", async () => {
    const text = await rejectedWith(() => tooMany(30));

    expect(text).toMatch(/слишком часто/i);
    expect(text).toMatch(/через 30 с/);
  });

  it("gives every Telegram HTML error code its own text, and never the raw code or the detail", async () => {
    const texts = new Map<string, string>();
    for (const code of HTML_CODES) texts.set(code, await rejectedWith(() => invalidHtml(code)));

    expect(new Set(texts.values()).size).toBe(HTML_CODES.length);
    for (const [code, text] of texts) {
      expect(text, code).not.toContain(SECRET_DETAIL);
      expect(text, code).not.toContain("telegram_html");
    }
  });

  it("shows no detail of a server error", async () => {
    const text = await rejectedWith(() => json({ detail: SECRET_DETAIL }, 500));

    expect(text).not.toContain(SECRET_DETAIL);
    expect(text.length).toBeGreaterThan(0);
  });
});

describe("broadcast form in English", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("speaks English: the form, the preview and the confirmation have no Russian", async () => {
    const user = userEvent.setup();
    const { container } = setup();
    await screen.findByRole("textbox", { name: /text/i });
    fireEvent.change(screen.getByRole("textbox", { name: /text/i }), {
      target: { value: "<b>Hello</b> 😀" },
    });
    await user.click(await screen.findByRole("button", { name: /^Send to 12 recipients$/i }));
    await screen.findByRole("alertdialog");

    expect(container.textContent).not.toMatch(/[А-Яа-яЁё]/);
    expect(screen.getByRole("alertdialog").textContent).not.toMatch(/[А-Яа-яЁё]/);
  });

  it("uses the English plural for one recipient", async () => {
    setup(OPERATOR, { respond: { [`POST ${BROADCASTS}/audience`]: audienceOf(1) } });
    fireEvent.change(await screen.findByRole("textbox", { name: /text/i }), { target: { value: "Hello" } });

    expect(await screen.findByRole("button", { name: /^Send to 1 recipient$/i })).toBeInTheDocument();
  });
});
