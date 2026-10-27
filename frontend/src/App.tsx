import { Navigate, Outlet, createBrowserRouter } from "react-router";

import { AuthProvider } from "./auth/AuthContext";
import { RequireAuth } from "./auth/RequireAuth";
import { AppShell } from "./components/AppShell";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";

/** Сессия админа восстанавливается только на маршрутах панели: публичная страница конфига (/c) её не трогает. */
function WithAuth() {
  return (
    <AuthProvider>
      <Outlet />
    </AuthProvider>
  );
}

export const router = createBrowserRouter([
  {
    // Страница конфига для клиента: без входа, без AuthProvider и каркаса панели. Токен берётся из фрагмента адреса.
    path: "/c",
    lazy: async () => ({ Component: (await import("./pages/public/PublicConfigPage")).PublicConfigPage }),
  },
  {
    element: <WithAuth />,
    children: [
      { path: "/login", element: <LoginPage /> },
      {
        element: <RequireAuth />,
        children: [
          {
            element: <AppShell />,
            children: [
              { index: true, element: <Navigate to="/servers" replace /> },
              // Каждый раздел — отдельным файлом: открытая страница не тянет код остальных (в основном бандле —
              // каркас, вход и словари). Тяжёлое внутри раздела (QR-коды, графики) грузится ещё позже.
              {
                path: "/servers",
                lazy: async () => ({ Component: (await import("./pages/ServersPage")).ServersPage }),
              },
              {
                path: "/servers/:serverId",
                lazy: async () => ({ Component: (await import("./pages/server/ServerPage")).ServerPage }),
              },
              {
                path: "/users",
                lazy: async () => ({ Component: (await import("./pages/UsersPage")).UsersPage }),
              },
              {
                path: "/users/:userId",
                lazy: async () => ({ Component: (await import("./pages/user/UserPage")).UserPage }),
              },
              {
                path: "/audit",
                lazy: async () => ({ Component: (await import("./pages/AuditPage")).AuditPage }),
              },
              { path: "*", element: <NotFoundPage /> },
            ],
          },
        ],
      },
    ],
  },
]);
