import "@fontsource-variable/golos-text";
import "./styles/global.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router";

import { router } from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { applyTheme, loadTheme } from "./lib/theme";

applyTheme(loadTheme());

const queryClient = new QueryClient({
  defaultOptions: {
    // 401/403 не повторяем: повтор не поможет. Сетевые сбои — один повтор.
    queries: {
      retry: (count, error) =>
        count < 1 && !("status" in error && [401, 403].includes(error.status as number)),
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
