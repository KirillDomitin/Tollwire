import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";

import { AuthContext } from "../auth/AuthContext";

interface Options {
  /** Права текущего админа; по умолчанию — суперадмин (может всё). */
  permissions?: string[] | "all";
  /** Начальный адрес и шаблон маршрута — для экранов с параметрами (/servers/:serverId). */
  route?: string;
  path?: string;
}

/** Экран со всем, что ему нужно: кэш запросов, роутер и вошедший админ с заданными правами. */
export function renderWithProviders(ui: ReactNode, { permissions = "all", route = "/", path }: Options = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const auth = {
    state: { status: "checking" as const },
    login: async () => {},
    logout: async () => {},
    changePassword: async () => {},
    can: (permission: string) => permissions === "all" || permissions.includes(permission),
  };
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={[route]}>
          {path ? (
            <Routes>
              <Route path={path} element={ui} />
              <Route path="*" element={<p>другая страница</p>} />
            </Routes>
          ) : (
            ui
          )}
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
  // queryClient - чтобы тест мог проверить, какие запросы экран объявил устаревшими.
  return { ...rendered, queryClient };
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
