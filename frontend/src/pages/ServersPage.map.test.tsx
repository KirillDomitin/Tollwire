import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { Server } from "../api/client";
import i18n from "../i18n";
import { json, renderWithProviders } from "../test/render";
import { ServersPage } from "./ServersPage";

function server(name: string, locationCityId: string | null, status: Server["status"] = "ready") {
  return {
    id: crypto.randomUUID(),
    name,
    host: `${name}.example.test`,
    ssh_port: 22,
    status,
    health_status: "ok",
    created_at: "2026-11-01T09:00:00Z",
    location_city_id: locationCityId,
  };
}

function renderServers(...servers: ReturnType<typeof server>[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) =>
      json(new URL(request.url).pathname === "/api/v1/servers" ? servers : []),
    ),
  );
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 620, 260));
  renderWithProviders(<ServersPage />, { permissions: ["servers:read"] });
}

function row(name: string) {
  return within(screen.getByRole("table")).getByRole("link", { name }).closest("tr");
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Server map", () => {
  it("draws a third red for an unreachable server and two thirds green for running servers", async () => {
    await i18n.changeLanguage("en");
    renderServers(
      server("swe-1", "se-stockholm"),
      server("swe-2", "se-stockholm"),
      server("fin-1", "fi-helsinki", "offline"),
    );

    const map = await screen.findByRole("group", { name: "World map" });
    const marker = within(map).getByRole("button", { name: /Cities grouped.*3/ });
    expect(within(marker).getByText("3")).toBeInTheDocument();
    const sectorLength = (signal: string) =>
      Number.parseFloat(
        marker.querySelector(`circle[data-signal="${signal}"]`)?.getAttribute("stroke-dasharray") ?? "",
      );
    expect(sectorLength("down")).toBeCloseTo(100 / 3, 2);
    expect(sectorLength("ok")).toBeCloseTo(200 / 3, 2);

    await userEvent.click(marker);
    expect(marker).toHaveAttribute("aria-pressed", "true");
    const details = within(screen.getByRole("group", { name: "Selected servers" }));
    expect(details.getByText("Unreachable")).toBeInTheDocument();
    expect(details.getAllByText("Running")).toHaveLength(2);
    for (const name of ["swe-1", "swe-2", "fin-1"])
      expect(row(name)).toHaveAttribute("data-map-selected", "true");
  });

  it("separates nearby cities with keyboard zoom and groups them again on zoom out", async () => {
    await i18n.changeLanguage("en");
    const user = userEvent.setup();
    renderServers(
      server("swe-1", "se-stockholm"),
      server("swe-2", "se-stockholm"),
      server("fin-1", "fi-helsinki"),
    );

    const map = await screen.findByRole("group", { name: "World map" });
    const group = within(map).getByRole("button", { name: /Cities grouped.*3/ });
    group.focus();
    await user.keyboard("{Enter}");
    expect(group).toHaveAttribute("aria-pressed", "true");

    const zoomIn = screen.getByRole("button", { name: "Zoom in" });
    zoomIn.focus();
    await user.keyboard("{Enter}{Enter}{Enter}{Enter}");
    expect(screen.getByText("16×")).toBeInTheDocument();
    expect(within(map).queryByRole("button", { name: /Cities grouped/ })).not.toBeInTheDocument();
    expect(within(map).getByRole("button", { name: /Helsinki/ })).toBeInTheDocument();
    const stockholm = within(map).getByRole("button", { name: /Stockholm.*2/ });
    await user.click(stockholm);
    expect(stockholm).toHaveAttribute("aria-pressed", "true");
    const details = within(screen.getByRole("group", { name: "Selected servers" }));
    expect(details.getByText("Stockholm")).toBeInTheDocument();
    for (const name of ["swe-1", "swe-2"]) {
      expect(details.getByText(name)).toBeInTheDocument();
      expect(row(name)).toHaveAttribute("data-map-selected", "true");
    }
    expect(details.queryByText("fin-1")).not.toBeInTheDocument();
    expect(row("fin-1")).not.toHaveAttribute("data-map-selected", "true");

    const zoomOut = screen.getByRole("button", { name: "Zoom out" });
    zoomOut.focus();
    await user.keyboard("{Enter}{Enter}{Enter}{Enter}");
    expect(screen.getByText("1×")).toBeInTheDocument();
    expect(within(map).getByRole("button", { name: /Cities grouped.*3/ })).toBeInTheDocument();
  });

  it("shares the status filter and search with the table and keeps unlocated servers there", async () => {
    await i18n.changeLanguage("en");
    const user = userEvent.setup();
    renderServers(
      server("swe-1", "se-stockholm"),
      server("fin-1", "fi-helsinki", "offline"),
      server("unknown-1", null, "offline"),
      server("unknown-catalog", "us-missing-from-catalog"),
    );

    expect(await screen.findByText("2 of 4 on the map")).toBeInTheDocument();
    expect(row("unknown-1")).toBeInTheDocument();
    expect(row("unknown-catalog")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Offline" }));
    expect(screen.getByText("1 of 2 on the map")).toBeInTheDocument();
    const map = screen.getByRole("group", { name: "World map" });
    expect(within(map).getByRole("button", { name: /Helsinki/ })).toBeInTheDocument();
    expect(within(screen.getByRole("table")).queryByRole("link", { name: "swe-1" })).not.toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search by name or address" }), "unknown-1");
    expect(screen.getByText("0 of 1 on the map")).toBeInTheDocument();
    expect(within(map).queryByRole("button", { name: /Helsinki/ })).not.toBeInTheDocument();
    expect(within(screen.getByRole("table")).queryByRole("link", { name: "fin-1" })).not.toBeInTheDocument();
    expect(row("unknown-1")).toBeInTheDocument();
  });

  it("localizes the map and city marker in Russian", async () => {
    renderServers(server("fin-1", "fi-helsinki"));

    const map = await screen.findByRole("group", { name: "Карта мира" });
    expect(screen.getByText("Карта серверов")).toBeInTheDocument();
    const marker = within(map).getByRole("button", { name: /Хельсинки/ });
    await userEvent.click(marker);
    expect(marker).toHaveAttribute("aria-pressed", "true");
    const details = within(screen.getByRole("group", { name: "Выбранные серверы" }));
    expect(details.getByText("В работе")).toBeInTheDocument();
  });

  it("starts collapsed at mobile width and opens with the map control", async () => {
    await i18n.changeLanguage("en");
    const originalMatchMedia = window.matchMedia.bind(window);
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      ...originalMatchMedia(query),
      matches: query === "(max-width: 767px)",
    }));
    renderServers(server("fin-1", "fi-helsinki"));

    const toggle = await screen.findByRole("button", { name: /Server map/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("group", { name: "World map" })).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("group", { name: "World map" })).toBeInTheDocument();
  });

  it.each(["mouse", "touch"])("drags a zoomed map with a %s pointer", async (pointerType) => {
    await i18n.changeLanguage("en");
    renderServers(server("sin-1", "sg-singapore"));

    const map = await screen.findByRole("group", { name: "World map" });
    await userEvent.click(within(map).getByRole("button", { name: /Singapore/ }));
    await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    const marker = within(map).getByRole("button", { name: /Singapore/ });
    const previousX = Number.parseFloat(marker.style.left);

    const setPointerCapture = vi.fn();
    Object.assign(map, { setPointerCapture, releasePointerCapture: vi.fn(), hasPointerCapture: () => true });
    const pointer = { pointerId: 1, pointerType, isPrimary: true, button: 0, clientY: 100 };
    fireEvent.pointerDown(map, { ...pointer, clientX: 100 });
    fireEvent.pointerMove(map, { ...pointer, clientX: 140 });
    fireEvent.pointerUp(map, { ...pointer, clientX: 140 });

    expect(setPointerCapture).toHaveBeenCalledWith(pointer.pointerId);
    expect(Number.parseFloat(marker.style.left)).toBeGreaterThan(previousX);
  });
});
