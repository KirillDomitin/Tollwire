import { Navigate, createBrowserRouter } from "react-router";

import { RequireAuth } from "./auth/RequireAuth";
import { AppShell } from "./components/AppShell";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";

export const router = createBrowserRouter([
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
]);
