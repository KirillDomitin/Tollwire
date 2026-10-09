import { render, screen } from "@testing-library/react";
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
});
