import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Локальная разработка: фронтенд и API — один origin (localhost:5173), /api проксируется на uvicorn.
// Так refresh-cookie (HttpOnly, path=/api/v1/auth) работает точно так же, как в проде за nginx, и CORS не нужен.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: false },
    },
  },
  // vite preview — собранная версия с той же политикой CSP, что отдаёт nginx в проде: так ловится всё,
  // что CSP заблокирует (инлайн-стили, внешние шрифты), ещё до деплоя.
  preview: {
    port: 4173,
    headers: {
      "Content-Security-Policy":
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; " +
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    },
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: {
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: { modules: { classNameStrategy: "non-scoped" } },
  },
});
