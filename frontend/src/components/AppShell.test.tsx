import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import { AuthContext } from "../auth/AuthContext";
import { AppShell } from "./AppShell";

function renderShell(permissions: string[]) {
  const auth = {
    state: {
      status: "signed-in" as const,
      admin: { id: "a1", email: "viewer@example.com", role: "viewer", permissions },
    },
    login: async () => {},
    logout: async () => {},
    can: (permission: string) => permissions.includes(permission),
  };
  return render(
    <AuthContext.Provider value={auth as never}>
      <MemoryRouter>
        <AppShell />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

describe("AppShell navigation", () => {
  it("links to the audit journal for those who may read it", () => {
    renderShell(["servers:read", "audit:read"]);

    expect(screen.getByRole("link", { name: "Журнал" })).toHaveAttribute("href", "/audit");
  });

  it("hides the audit journal from those who may not read it", () => {
    renderShell(["servers:read"]);

    expect(screen.queryByRole("link", { name: "Журнал" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Серверы" })).toBeInTheDocument();
  });

  it("offers to change the password from the user menu", async () => {
    renderShell(["servers:read"]);

    expect(screen.queryByRole("menuitem", { name: "Сменить пароль" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /viewer@example.com/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Сменить пароль" }));

    expect(await screen.findByRole("dialog", { name: "Смена пароля" })).toBeInTheDocument();
    expect(screen.getByLabelText("Текущий пароль")).toBeInTheDocument();
  });

  it("opens the user menu with the language choice and sign-out, and closes it on Escape", async () => {
    renderShell(["servers:read"]);

    const trigger = screen.getByRole("button", { name: /viewer@example.com/ });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Наблюдатель")).toBeInTheDocument();

    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menuitemradio", { name: /Русский/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("menuitemradio", { name: /English/ })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("menuitem", { name: "Выйти" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
  });

  it("has no theme switch: the interface is dark only", () => {
    renderShell(["servers:read"]);

    expect(screen.queryByRole("radio", { name: /тема|светл/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открыть меню" })).toBeInTheDocument();
  });
});
