import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import i18n from "../../i18n";
import { json, renderWithProviders } from "../../test/render";
import { AddServerDialog } from "./AddServerDialog";
import { EditServerDialog } from "./EditServerDialog";

const server = {
  id: "8c4eb385-896c-4ee2-bfd4-97e0bc3d941b",
  name: "nordic-exit",
  host: "sample.example.test",
  ssh_port: 22,
  status: "ready" as const,
  created_at: "2026-11-01T09:00:00Z",
};

function saveServer() {
  const writes: { method: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      writes.push({ method: request.method, body: await request.json() });
      return json(server);
    }),
  );
  return writes;
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it.each([
  { city: "", expected: {} },
  { city: "fi-helsinki", expected: { location_city_id: "fi-helsinki" } },
])("includes the selected city on add and omits an empty selection ($city)", async ({ city, expected }) => {
  const user = userEvent.setup();
  const writes = saveServer();
  renderWithProviders(<AddServerDialog />);
  await user.click(screen.getByRole("button", { name: "Add server" }));
  const dialog = within(screen.getByRole("dialog"));
  const select = dialog.getByRole("combobox", { name: "City (optional)" });

  expect(select).toHaveValue("");
  expect(within(select).getByRole("option", { name: "Helsinki, Finland" })).toBeInTheDocument();
  await user.type(dialog.getByRole("textbox", { name: "Name in the panel" }), server.name);
  await user.type(dialog.getByRole("textbox", { name: "Address (IP or domain)" }), server.host);
  if (city) await user.selectOptions(select, city);
  await user.click(dialog.getByRole("button", { name: "Add" }));

  await waitFor(() =>
    expect(writes).toEqual([
      {
        method: "POST",
        body: { name: server.name, host: server.host, ssh_port: 22, description: null, ...expected },
      },
    ]),
  );
});

it.each([
  { saved: "se-stockholm", selected: "fi-helsinki", expected: { location_city_id: "fi-helsinki" } },
  { saved: "se-stockholm", selected: "", expected: { location_city_id: null } },
  { saved: "se-not-in-catalog", selected: undefined, expected: {} },
])(
  "preserves, replaces, or explicitly clears a saved city ($saved → $selected)",
  async ({ saved, selected, expected }) => {
    const user = userEvent.setup();
    const writes = saveServer();
    const existing = { ...server, location_city_id: saved };
    renderWithProviders(<EditServerDialog server={existing} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = within(screen.getByRole("dialog"));
    const select = dialog.getByRole("combobox", { name: "City (optional)" });

    expect(select).toHaveValue(saved);
    if (selected !== undefined) await user.selectOptions(select, selected);
    await user.click(dialog.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(writes).toEqual([
        { method: "PATCH", body: { name: server.name, description: null, ...expected } },
      ]),
    );
  },
);
