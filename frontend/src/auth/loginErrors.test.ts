import { loginErrorMessage } from "./AuthContext";

it.each([
  [401, undefined, "Неверный email или пароль."],
  [429, 898, "Слишком много попыток входа. Повторите через 15 мин."],
  [429, 20, "Слишком много попыток входа. Повторите через 1 мин."],
  [429, undefined, "Слишком много попыток входа. Повторите позже."],
  [500, undefined, "Сервер панели ответил ошибкой. Повторите позже."],
])("status %i (retry %s) → %s", (status, retryAfter, message) => {
  expect(loginErrorMessage(status, retryAfter)).toBe(message);
});
