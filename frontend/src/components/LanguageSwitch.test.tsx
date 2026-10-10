import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../i18n";
import { renderWithProviders } from "../test/render";
import { LoginPage } from "../pages/LoginPage";
import { LanguageSwitch } from "./LanguageSwitch";

afterEach(() => {
  localStorage.clear();
});

describe("LanguageSwitch", () => {
  it("offers both languages and marks the current one", () => {
    renderWithProviders(<LanguageSwitch />);
    expect(screen.getByRole("radio", { name: "RU" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "EN" })).not.toBeChecked();
  });

  it("switches the interface language and remembers the choice", async () => {
    renderWithProviders(<LoginPage />);
    expect(screen.getByRole("heading", { name: "Вход в панель" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "EN" }));

    expect(await screen.findByRole("heading", { name: "Sign in to the panel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
    expect(localStorage.getItem("vpn-panel-lang")).toBe("en");
    expect(i18n.resolvedLanguage).toBe("en");
  });
});
