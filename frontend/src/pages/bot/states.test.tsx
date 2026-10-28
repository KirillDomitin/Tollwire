import { screen } from "@testing-library/react";
import type { ComponentType } from "react";

import { EMPTY_TEXT, SECRET_DETAIL, SUPERADMIN, mockBot, never, type BotBackend } from "../../test/botApi";
import { json, renderWithProviders } from "../../test/render";
import { BroadcastsPage } from "./BroadcastsPage";
import { InvitesPage } from "./InvitesPage";
import { RegistrationsPage } from "./RegistrationsPage";
import { SettingsPage } from "./SettingsPage";
import { StatusPage } from "./StatusPage";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4): состояния каждого раздела страницы «Бот» одинаковы.
 * - Загрузка: текст «Загрузка…» в элементе (или внутри элемента) с aria-busy="true"; пустого состояния в это время нет.
 * - Ошибка запроса: Notice (role="alert"), текст выбирается по статусу; `ApiError.detail` (английский текст
 *   бэкенда) на экран не попадает.
 * - Пусто: своё пустое состояние - «Ключей пока нет», «Приглашений пока нет», «Регистраций пока нет»,
 *   «Рассылок пока нет» (допустимо и «Нет …»; см. EMPTY_TEXT в src/test/botApi.ts). У настроек пустого состояния нет.
 */

interface Section {
  name: string;
  Page: ComponentType;
  empty?: { text: RegExp; backend: BotBackend };
}

const SECTIONS: Section[] = [
  { name: "Состояние", Page: StatusPage, empty: { text: EMPTY_TEXT.keys, backend: { keys: [] } } },
  { name: "Приглашения", Page: InvitesPage, empty: { text: EMPTY_TEXT.invites, backend: { invites: [] } } },
  {
    name: "Регистрации",
    Page: RegistrationsPage,
    empty: { text: EMPTY_TEXT.registrations, backend: { registrations: [] } },
  },
  {
    name: "Рассылки",
    Page: BroadcastsPage,
    empty: { text: EMPTY_TEXT.broadcasts, backend: { broadcasts: [] } },
  },
  { name: "Настройки", Page: SettingsPage },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe.each(SECTIONS)("bot section «$name»", ({ Page, empty }) => {
  it("shows a busy «Загрузка…» while the data is on its way", async () => {
    mockBot({ permissions: SUPERADMIN, respondAll: never });
    renderWithProviders(<Page />, { permissions: SUPERADMIN });

    const loading = (await screen.findAllByText("Загрузка…"))[0] as HTMLElement;

    expect(loading.closest("[aria-busy='true']")).not.toBeNull();
    if (empty) expect(screen.queryByText(empty.text)).not.toBeInTheDocument();
  });

  it.each([
    [500, "a server error"],
    [403, "a refusal"],
    [404, "a missing object"],
  ])("shows a notice, not the backend detail, on %i (%s)", async (status) => {
    mockBot({
      permissions: SUPERADMIN,
      respondAll: () => json({ detail: SECRET_DETAIL }, status),
    });
    renderWithProviders(<Page />, { permissions: SUPERADMIN });

    const alerts = await screen.findAllByRole("alert");

    for (const alert of alerts) expect(alert).not.toHaveTextContent(SECRET_DETAIL);
    expect(document.body.textContent).not.toContain(SECRET_DETAIL);
    expect(screen.queryByText("Загрузка…")).not.toBeInTheDocument();
  });

  it("shows a notice when the connection fails", async () => {
    mockBot({
      permissions: SUPERADMIN,
      respondAll: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    renderWithProviders(<Page />, { permissions: SUPERADMIN });

    const alerts = await screen.findAllByRole("alert");

    expect(alerts.length).toBeGreaterThan(0);
  });

  if (empty) {
    it("has an empty state of its own", async () => {
      mockBot({ permissions: SUPERADMIN, ...empty.backend });
      renderWithProviders(<Page />, { permissions: SUPERADMIN });

      expect(await screen.findByText(empty.text)).toBeInTheDocument();
      expect(screen.queryByText("Загрузка…")).not.toBeInTheDocument();
    });
  }
});
