import "@fontsource-variable/golos-text";
import "./styles/global.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { useTranslation } from "react-i18next";
import { RouterProvider } from "react-router";

import { router } from "./App";
import { AuthProvider } from "./auth/AuthContext";
import "./i18n";

const queryClient = new QueryClient({
  defaultOptions: {
    // 401/403 не повторяем: повтор не поможет. Сетевые сбои — один повтор.
    queries: {
      retry: (count, error) =>
        count < 1 && !("status" in error && [401, 403].includes(error.status as number)),
    },
  },
});

/** Смена языка пересоздаёт интерфейс: даты, числа и подписи, посчитанные вне компонентов, обновляются разом. */
function LocalizedRouter() {
  const { i18n } = useTranslation();
  return <RouterProvider key={i18n.resolvedLanguage} router={router} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <LocalizedRouter />
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
