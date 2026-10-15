import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { getAccessToken, setAccessToken } from "../api/session";
import { json } from "../test/render";
import { AuthProvider, changePasswordErrorMessage, useAuth } from "./AuthContext";

const admin = (mustChange: boolean) => ({
  id: "a1",
  email: "tmp@example.com",
  is_active: true,
  role: "viewer",
  must_change_password: mustChange,
  permissions: [],
  is_superuser: false,
  created_at: "2026-10-15T00:00:00Z",
});

function Probe() {
  const { state, changePassword } = useAuth();
  const [error, setError] = useState("");
  const label =
    state.status === "signed-in" ? (state.admin.must_change_password ? "temporary" : "own") : state.status;
  return (
    <>
      <p data-testid="state">{label}</p>
      <button
        onClick={() =>
          changePassword("old-password-1", "new-password-123").catch((e: Error) => setError(e.message))
        }
      >
        go
      </button>
      <p data-testid="error">{error}</p>
    </>
  );
}

function mount() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

interface Call {
  url: string;
  auth: string | null;
  body: string | null;
}

/** Подменённый сервер: refresh выдаёт t1, t2…; смену пароля обрабатывает handler. */
function stubServer(handleChange: (call: Call, attempt: number) => Response) {
  const calls: Call[] = [];
  let refreshes = 0;
  let changes = 0;
  let mustChange = true;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.url;
      const headers = new Headers(typeof input === "string" ? init?.headers : input.headers);
      const call = {
        url,
        auth: headers.get("Authorization"),
        body: typeof init?.body === "string" ? init.body : null,
      };
      calls.push(call);
      if (url.endsWith("/auth/refresh")) return json({ access_token: `t${++refreshes}` });
      if (url.endsWith("/auth/me")) return json(admin(mustChange));
      if (url.endsWith("/auth/change-password")) {
        const response = handleChange(call, ++changes);
        if (response.ok) mustChange = false;
        return response;
      }
      throw new Error(`unexpected ${url}`);
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

describe("changePassword", () => {
  it("sends both passwords with the current token, then takes the new token and the refreshed profile", async () => {
    const calls = stubServer(() => json({ access_token: "fresh" }));
    mount();
    expect(await screen.findByText("temporary")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "go" }));

    expect(await screen.findByText("own")).toBeInTheDocument(); // профиль перечитан: флаг временного пароля снят
    const change = calls.find((call) => call.url.endsWith("/auth/change-password"));
    expect(change?.auth).toBe("Bearer t1");
    expect(JSON.parse(change?.body ?? "{}")).toEqual({
      current_password: "old-password-1",
      new_password: "new-password-123",
    });
    expect(getAccessToken()).toBe("fresh");
  });

  it("refreshes an expired access token once and repeats the change", async () => {
    // Админ с временным паролем мог просидеть над формой дольше жизни access-токена (15 минут).
    const calls = stubServer((call, attempt) =>
      attempt === 1 ? json({ detail: "x" }, 401) : json({ access_token: `after-${call.auth}` }),
    );
    mount();
    await screen.findByText("temporary");

    await userEvent.click(screen.getByRole("button", { name: "go" }));

    expect(await screen.findByText("own")).toBeInTheDocument();
    const changes = calls.filter((call) => call.url.endsWith("/auth/change-password"));
    expect(changes.map((call) => call.auth)).toEqual(["Bearer t1", "Bearer t2"]);
  });

  it("tells a wrong current password apart from an expired session and stays on the temporary password", async () => {
    stubServer(() => json({ detail: "Current password is incorrect" }, 400));
    mount();
    await screen.findByText("temporary");

    await userEvent.click(screen.getByRole("button", { name: "go" }));

    expect(await screen.findByText("Текущий пароль неверен.")).toBeInTheDocument();
    expect(screen.getByTestId("state")).toHaveTextContent("temporary");
  });

  it("reports a lost connection", async () => {
    stubServer(() => {
      throw new TypeError("Failed to fetch");
    });
    mount();
    await screen.findByText("temporary");

    await userEvent.click(screen.getByRole("button", { name: "go" }));

    expect(await screen.findByText(/Нет связи с сервером панели/)).toBeInTheDocument();
  });
});

it.each([
  [400, undefined, "Текущий пароль неверен."],
  [422, undefined, "Новый пароль должен быть не короче 12 символов и отличаться от текущего."],
  [429, 898, "Слишком много неверных попыток. Повторите через 15 мин."],
  [429, undefined, "Слишком много неверных попыток. Повторите позже."],
  [401, undefined, "Сессия закончилась. Войдите снова."],
  [500, undefined, "Сервер панели ответил ошибкой. Повторите позже."],
])("status %i (retry %s) → %s", (status, retryAfter, message) => {
  expect(changePasswordErrorMessage(status, retryAfter)).toBe(message);
});
