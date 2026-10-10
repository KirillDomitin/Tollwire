import { Navigate, Outlet, createBrowserRouter, useLocation } from "react-router";

import { useAuth } from "./auth/AuthContext";
import { AppShell } from "./components/AppShell";
import { BrandMark } from "./components/BrandMark";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";

/** Пускает дальше только вошедшего админа; пока сессия восстанавливается — экран ожидания. */
function RequireAuth() {
  const { state } = useAuth();
  const location = useLocation();
  if (state.status === "checking") {
    return (
      <div style={{ display: "grid", placeItems: "center", height: "100%" }} aria-busy="true">
        <BrandMark />
      </div>
    );
  }
  if (state.status === "signed-out") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <Outlet />;
}

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
