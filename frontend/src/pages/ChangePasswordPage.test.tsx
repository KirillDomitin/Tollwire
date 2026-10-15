import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";

import { ApiError } from "../api/client";
import { AuthContext } from "../auth/AuthContext";
import i18n from "../i18n";
import { RequireAuth } from "../auth/RequireAuth";
import { ChangePasswordPage } from "./ChangePasswordPage";

function auth(
  mustChange: boolean,
  changePassword: (current: string, next: string) => Promise<void> = async () => {},
) {
  const change = vi.fn(changePassword);
  return {
    changePassword: change,
    logout: vi.fn(async () => {}),
    value: {
      state: {
        status: "signed-in" as const,
        admin: {
          id: "a1",
          email: "tmp@example.com",
          role: "viewer",
          permissions: [],
          must_change_password: mustChange,
        },
      },
      login: async () => {},
      logout: vi.fn(async () => {}),
      changePassword: change,
      can: () => true,
    },
  };
}

function renderWith(value: unknown, ui: ReactNode) {
  return render(
    <AuthContext.Provider value={value as never}>
      <MemoryRouter>{ui}</MemoryRouter>
    </AuthContext.Provider>,
  );
}

async function fill(current: string, next: string, repeat: string) {
  await userEvent.type(screen.getByLabelText("Текущий (временный) пароль"), current);
  await userEvent.type(screen.getByLabelText("Новый пароль"), next);
  await userEvent.type(screen.getByLabelText("Повторите новый пароль"), repeat);
  await userEvent.click(screen.getByRole("button", { name: "Сохранить пароль" }));
}

describe("RequireAuth with a temporary password", () => {
  const tree = (
    <Routes>
      <Route element={<RequireAuth />}>
        <Route path="/" element={<p>панель</p>} />
      </Route>
    </Routes>
  );

  it("shows only the password screen instead of the panel", () => {
    renderWith(auth(true).value, tree);

    expect(screen.getByRole("heading", { name: "Задайте свой пароль" })).toBeInTheDocument();
    expect(screen.queryByText("панель")).not.toBeInTheDocument();
  });

  it("lets the admin with their own password into the panel", () => {
    renderWith(auth(false).value, tree);

    expect(screen.getByText("панель")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Задайте свой пароль" })).not.toBeInTheDocument();
  });

  it("lets the admin sign out instead of changing the password", async () => {
    const { value } = auth(true);
    renderWith(value, tree);

    await userEvent.click(screen.getByRole("button", { name: "Выйти" }));

    expect(value.logout).toHaveBeenCalled();
  });
});

describe("ChangePasswordPage form", () => {
  it("sends the current and the new password", async () => {
    const { value, changePassword } = auth(true);
    renderWith(value, <ChangePasswordPage />);

    await fill("temporary-pass-1", "my-own-password-1", "my-own-password-1");

    expect(changePassword).toHaveBeenCalledWith("temporary-pass-1", "my-own-password-1");
  });

  it.each([
    ["short", "short", "short", "Новый пароль — не короче 12 символов."],
    ["temporary-pass-1", "my-own-password-1", "my-own-password-2", "Пароли не совпадают."],
    [
      "temporary-pass-1",
      "temporary-pass-1",
      "temporary-pass-1",
      "Новый пароль должен отличаться от текущего.",
    ],
  ])(
    "rejects %s / %s / %s on the page without calling the server",
    async (current, next, repeat, message) => {
      const { value, changePassword } = auth(true);
      renderWith(value, <ChangePasswordPage />);

      await fill(current, next, repeat);

      expect(screen.getByRole("alert")).toHaveTextContent(message);
      expect(changePassword).not.toHaveBeenCalled();
    },
  );

  it("shows the server reason and keeps the form for another try", async () => {
    const { value } = auth(true, async () => {
      throw new ApiError("Текущий пароль неверен.", 400);
    });
    renderWith(value, <ChangePasswordPage />);

    await fill("wrong-password-1", "my-own-password-1", "my-own-password-1");

    expect(await screen.findByRole("alert")).toHaveTextContent("Текущий пароль неверен.");
    expect(screen.getByRole("button", { name: "Сохранить пароль" })).toBeEnabled();
  });
});

describe("ChangePasswordPage language", () => {
  it("follows the interface language", async () => {
    await i18n.changeLanguage("en");
    renderWith(auth(true).value, <ChangePasswordPage />);

    expect(screen.getByRole("heading", { name: "Set your own password" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current (temporary) password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save password" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("explains client-side problems in English too", async () => {
    await i18n.changeLanguage("en");
    renderWith(auth(true).value, <ChangePasswordPage />);

    await userEvent.type(screen.getByLabelText("Current (temporary) password"), "temporary-pass-1");
    await userEvent.type(screen.getByLabelText("New password"), "short");
    await userEvent.type(screen.getByLabelText("Repeat the new password"), "short");
    await userEvent.click(screen.getByRole("button", { name: "Save password" }));

    expect(screen.getByRole("alert")).toHaveTextContent("The new password must be at least 12 characters.");
  });
});
