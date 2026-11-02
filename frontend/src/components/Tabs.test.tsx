import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { TabPanel, Tabs } from "./Tabs";

const TABS = [
  { id: "charts", label: "Графики" },
  { id: "config", label: "Конфигурация" },
  { id: "settings", label: "Настройки" },
];

function Example() {
  const [value, setValue] = useState("charts");
  return (
    <>
      <Tabs idPrefix="t" label="Разделы" tabs={TABS} value={value} onChange={setValue} />
      <TabPanel idPrefix="t" id="charts" value={value}>
        панель графиков
      </TabPanel>
      <TabPanel idPrefix="t" id="config" value={value}>
        панель конфигурации
      </TabPanel>
    </>
  );
}

describe("Tabs", () => {
  it("is a tablist with one selected tab linked to its panel", () => {
    render(<Example />);

    expect(screen.getByRole("tablist", { name: "Разделы" })).toBeInTheDocument();
    const charts = screen.getByRole("tab", { name: "Графики" });
    expect(charts).toHaveAttribute("aria-selected", "true");
    expect(charts).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "Конфигурация" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Графики");
    expect(screen.queryByText("панель конфигурации")).not.toBeInTheDocument();
  });

  it("switches on click and with the arrow, Home and End keys", async () => {
    render(<Example />);

    await userEvent.click(screen.getByRole("tab", { name: "Конфигурация" }));
    expect(screen.getByText("панель конфигурации")).toBeInTheDocument();

    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Настройки" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Настройки" })).toHaveFocus();

    await userEvent.keyboard("{ArrowRight}"); // по кругу
    expect(screen.getByRole("tab", { name: "Графики" })).toHaveAttribute("aria-selected", "true");

    await userEvent.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Настройки" })).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "Графики" })).toHaveFocus();
  });
});
