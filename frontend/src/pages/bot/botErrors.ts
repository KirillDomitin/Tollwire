import { ApiError, unwrap } from "../../api/client";
import i18n from "../../i18n";
import { TELEGRAM_HTML_CODES } from "../../lib/telegramHtml";

/**
 * Что сказать человеку об отказе: текст выбирается по статусу, причине 409 (`reason`) и коду проверки 422
 * (`telegram_html_*`). `ApiError.detail` - английская фраза бэкенда - на экран не попадает никогда.
 */
export function botErrorText(exc: ApiError): string {
  if (exc.status === 429 && exc.retryAfterSeconds) {
    return i18n.t("bot.errors.tooMany", { seconds: exc.retryAfterSeconds });
  }
  if (exc.status === 409) {
    const path = `bot.errors.reasons.${exc.reason ?? ""}`;
    return exc.reason && i18n.exists(path)
      ? i18n.t(path as "bot.errors.conflict")
      : i18n.t("bot.errors.conflict");
  }
  if (exc.status === 422) {
    const code = TELEGRAM_HTML_CODES.find((item) => item === exc.code);
    if (code) return i18n.t(`bot.html.${code.replace("telegram_html_", "")}` as "bot.html.tag");
    return i18n.t("bot.errors.invalid");
  }
  return exc.message;
}

/** Как `unwrap`, но сообщение ApiError - уже готовый текст для экрана страницы «Бот» (429, 409, 422 - по-своему). */
export async function botUnwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
  messages: Partial<Record<number, string>> = {},
): Promise<T> {
  try {
    return await unwrap(call, messages);
  } catch (exc) {
    if (exc instanceof ApiError && !messages[exc.status]) {
      throw new ApiError(
        botErrorText(exc),
        exc.status,
        exc.retryAfterSeconds,
        exc.detail,
        exc.reason,
        exc.code,
      );
    }
    throw exc;
  }
}

/** Текст для Notice: сообщение ApiError или общий текст, если упало что-то иное. */
export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : i18n.t("common.errors.actionFailed");
}
