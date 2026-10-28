import { render } from "@testing-library/react";

import rawCases from "../../lib/telegramHtml.cases.json";
import { TelegramPreview } from "./TelegramPreview";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4):
 * - `src/pages/bot/TelegramPreview.tsx` экспортирует `TelegramPreview({ html }: { html: string })`: предпросмотр
 *   текста новости. Он строится из `parseTelegramHtml` (src/lib/telegramHtml.ts) React-элементами, без innerHTML.
 * - Предпросмотр - «как увидит человек»: для допустимого текста его textContent совпадает с видимым текстом
 *   (теги и сущности разобраны, лишних подписей нет); спойлер остаётся в DOM, прячется стилем.
 * - Текст, не прошедший проверку (`validateTelegramHtml(...).ok === false`), не разбирается: предпросмотр выводит
 *   исходную строку обычным текстом, ни одного тега из неё в DOM не появляется.
 * - Ссылки в предпросмотре не кликабельны: ни `<a>`, ни атрибута href; видна только подпись ссылки.
 * - Общий набор примеров - src/lib/telegramHtml.cases.json (формат: см. src/lib/telegramHtml.test.ts).
 */

interface Case {
  name: string;
  input: string;
  ok: boolean;
  visible_length?: number;
}
const CASES = rawCases as unknown as Case[];

/** Элементы, которых в предпросмотре быть не должно ни при каком тексте. */
const DANGEROUS =
  "script, img, iframe, object, embed, a, form, input, style, link, svg, math, base, meta, video, audio, source";
const UNSAFE_ATTRIBUTES = ["href", "src", "srcdoc", "style", "action", "formaction"];

function expectNothingExecutable(container: HTMLElement) {
  expect(container.querySelectorAll(DANGEROUS)).toHaveLength(0);
  for (const element of container.querySelectorAll("*")) {
    for (const attribute of Array.from(element.attributes)) {
      expect(attribute.name.startsWith("on"), `${element.tagName} has ${attribute.name}`).toBe(false);
      expect(UNSAFE_ATTRIBUTES, `${element.tagName} has ${attribute.name}`).not.toContain(attribute.name);
    }
  }
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TelegramPreview on the shared cases", () => {
  it.each(
    CASES.filter((item) => item.ok && item.visible_length !== undefined).map(
      (item) => [item.name, item] as const,
    ),
  )("%s: shows exactly the visible text", (_name, item) => {
    const { container } = render(<TelegramPreview html={item.input} />);

    expect(container.textContent?.length).toBe(item.visible_length);
  });

  it.each(CASES.map((item) => [item.name, item] as const))(
    "%s: puts nothing executable or clickable in the DOM",
    (_name, item) => {
      const { container } = render(<TelegramPreview html={item.input} />);

      expectNothingExecutable(container);
    },
  );
});

describe("TelegramPreview: markup from the white list", () => {
  it("shows bold, italic, underline, strike, code and quotes as elements with the text inside", () => {
    const { container } = render(
      <TelegramPreview
        html={
          "<b>bold</b> <i>italic</i> <u>under</u> <s>strike</s> <code>code</code> <blockquote>quote</blockquote>"
        }
      />,
    );

    expect(container.querySelector("b, strong")).toHaveTextContent("bold");
    expect(container.querySelector("i, em")).toHaveTextContent("italic");
    expect(container.querySelector("u, ins")).toHaveTextContent("under");
    expect(container.querySelector("s, strike, del")).toHaveTextContent("strike");
    expect(container.querySelector("code")).toHaveTextContent("code");
    expect(container.querySelector("blockquote")).toHaveTextContent("quote");
  });

  it("keeps the line breaks of the text", () => {
    const { container } = render(<TelegramPreview html={"first\nsecond"} />);

    expect(container.textContent).toBe("first\nsecond");
  });

  it("shows entities as the characters they stand for", () => {
    const { container } = render(<TelegramPreview html="a &amp; b &lt;c&gt;" />);

    expect(container.textContent).toBe("a & b <c>");
  });
});

describe("TelegramPreview: links are not clickable", () => {
  it("shows the label of a link and no link", () => {
    const { container } = render(<TelegramPreview html={'<a href="https://example.com/x">our site</a>'} />);

    expect(container).toHaveTextContent("our site");
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("[href]")).toBeNull();
  });
});

describe("TelegramPreview: hostile text", () => {
  it("prints <img onerror> as text instead of creating an element", () => {
    const hostile = '<img src=x onerror="alert(1)">';
    const { container } = render(<TelegramPreview html={hostile} />);

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.textContent).toContain(hostile);
  });

  it("prints <script> as text too, together with the harmless markup around it", () => {
    const hostile = "<b>ok</b><script>alert(1)</script>";
    const { container } = render(<TelegramPreview html={hostile} />);

    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("does not write user text through innerHTML", () => {
    const setter = vi.spyOn(Element.prototype, "innerHTML", "set");
    const { container } = render(
      <TelegramPreview html={'<img src=x onerror="alert(1)"><b>x</b><a href="javascript:alert(1)">y</a>'} />,
    );

    const written = setter.mock.calls.map((call) => String(call[0]));
    expect(written.filter((text) => /<(img|script|a)\b|onerror/i.test(text))).toEqual([]);
    expect(container.querySelector("img, script, a")).toBeNull();
  });

  it("copes with an empty text", () => {
    const { container } = render(<TelegramPreview html="" />);

    expectNothingExecutable(container);
  });
});
