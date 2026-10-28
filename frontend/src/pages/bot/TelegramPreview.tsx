import type { ReactNode } from "react";

import { parseTelegramHtml, type TelegramNode } from "../../lib/telegramHtml";
import styles from "./TelegramPreview.module.css";

function render(nodes: TelegramNode[]): ReactNode[] {
  return nodes.map((node, index) => {
    if (typeof node === "string") return node;
    const inner = render(node.children);
    switch (node.tag) {
      case "b":
        return <b key={index}>{inner}</b>;
      case "i":
        return <i key={index}>{inner}</i>;
      case "u":
        return <u key={index}>{inner}</u>;
      case "s":
        return <s key={index}>{inner}</s>;
      case "code":
        return <code key={index}>{inner}</code>;
      case "pre":
        return <pre key={index}>{inner}</pre>;
      case "blockquote":
        return <blockquote key={index}>{inner}</blockquote>;
      case "spoiler":
        // Спойлер остаётся в тексте, прячется стилем; наведение и фокус его показывают - админ видит, что внутри.
        return (
          <span key={index} className={styles.spoiler} tabIndex={0}>
            {inner}
          </span>
        );
      case "a":
        // Ссылка не кликабельна: ни <a>, ни href. Видна только подпись, подчёркнутая как ссылка в Telegram.
        return (
          <span key={index} className={styles.link}>
            {inner}
          </span>
        );
    }
  });
}

/**
 * Предпросмотр новости «как увидит человек». Строится из узлов `parseTelegramHtml` React-элементами из белого списка:
 * строка никогда не попадает в разметку как HTML (нет innerHTML), поэтому `<img onerror>` или `<script>` в тексте -
 * это просто текст. Текст, не прошедший проверку, выводится как есть, обычным текстом.
 */
export function TelegramPreview({ html }: { html: string }) {
  return <div className={styles.preview}>{render(parseTelegramHtml(html))}</div>;
}
